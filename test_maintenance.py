"""Проверки backup/restore на настоящей SQLite, включая WAL и аварийные случаи."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from core import Service
from main import bind_identity, metadata
from maintenance import (HOLD_KEY,PROJECT_KEY,activate,backup,project_id,require_active_db,
                         restore,snapshot,verify)
from max_webhook import Inbox,worker_tick
from transports import Event


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'live.sqlite3'
        self.archive = self.root/'copy.zip'
        self.target = self.root/'restored.sqlite3'
        self.service = Service(str(self.path))
        self.addCleanup(self.service.db.close)
        bind_identity(self.service,'max','123')
        require_active_db(self.service.db)
        self.inbox = Inbox(self.path)
        self.service.db.execute('PRAGMA wal_autocheckpoint=0')
        self.service.execute('max','-100','42','/help',True,'one',enqueue_reply=True)
        self.inbox.enqueue(Event('-100','42','/help','two'))
        metadata(self.service,'max:cursor',456,write=True)
        metadata(self.service,'max:webhook_heartbeat',9999999999,write=True)
        self.network = patch('socket.create_connection',side_effect=AssertionError('No external network'))
        self.network.start(); self.addCleanup(self.network.stop)

    def make_backup(self):
        return backup(self.path,self.archive)

    def change_archive(self, edit_manifest=None, edit_db=None, extra=None):
        with zipfile.ZipFile(self.archive) as z:
            manifest=json.loads(z.read('manifest.json')); data=z.read('database.sqlite3')
        if edit_manifest:edit_manifest(manifest)
        if edit_db:data=edit_db(data)
        with zipfile.ZipFile(self.archive,'w') as z:
            z.writestr('manifest.json',json.dumps(manifest));z.writestr('database.sqlite3',data)
            if extra:z.writestr(extra,'bad')

    def test_live_wal_backup_keeps_state_cursor_and_pending_queue(self):
        self.assertGreater(Path(str(self.path)+'-wal').stat().st_size,0)
        self.make_backup()
        self.assertTrue(verify(self.archive)['verified'])
        restore(self.archive,self.target)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT data FROM chats').fetchall(),self.service.db.execute('SELECT data FROM chats').fetchall())
            self.assertEqual(1,db.execute('SELECT count(*) FROM events').fetchone()[0])
            self.assertEqual('pending',db.execute('SELECT status FROM outbox').fetchone()[0])
            self.assertEqual('pending',db.execute('SELECT status FROM webhook_inbox').fetchone()[0])
            self.assertEqual('456',db.execute("SELECT value FROM transport_meta WHERE key='max:cursor'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT value FROM transport_meta WHERE key='max:webhook_heartbeat'").fetchone())
            with self.assertRaisesRegex(ValueError,'приостановлена'):require_active_db(db)

    def test_project_scenario_roundtrip(self):
        for i,(command,admin,expected) in enumerate(json.loads(Path(__file__).with_name('scenario.json').read_text())):
            self.assertIn(expected,self.service.execute('max','scenario','alice',command,admin,str(i)))
        before=self.service.db.execute('SELECT scope,data FROM chats ORDER BY scope').fetchall()
        self.make_backup();restore(self.archive,self.target)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(before,db.execute('SELECT scope,data FROM chats ORDER BY scope').fetchall())

    def test_backup_does_not_change_live_data_or_queue(self):
        before=list(self.service.db.iterdump())
        self.make_backup()
        self.assertEqual(before,list(self.service.db.iterdump()))

    def test_uncommitted_wal_change_is_not_in_snapshot(self):
        self.service.db.execute("UPDATE chats SET data='{\"uncommitted\":true}'")
        self.make_backup()
        self.service.db.rollback()
        restore(self.archive,self.target)
        with sqlite3.connect(self.target) as db:
            self.assertEqual(db.execute('SELECT data FROM chats').fetchall(),self.service.db.execute('SELECT data FROM chats').fetchall())

    def test_archive_and_restored_file_are_private(self):
        self.make_backup();restore(self.archive,self.target)
        for file in (self.archive,self.target):self.assertEqual(0o600,file.stat().st_mode & 0o777)
        with zipfile.ZipFile(self.archive) as z:self.assertEqual({'manifest.json','database.sqlite3'},set(z.namelist()))

    def test_missing_source_is_not_created(self):
        with self.assertRaises(ValueError):backup(self.root/'missing.sqlite3',self.archive)
        self.assertFalse((self.root/'missing.sqlite3').exists());self.assertFalse(self.archive.exists())

    def test_existing_output_never_overwritten(self):
        self.archive.write_bytes(b'keep archive')
        with self.assertRaises(ValueError):self.make_backup()
        self.assertEqual(b'keep archive',self.archive.read_bytes())
        self.archive.unlink();self.make_backup();self.target.write_bytes(b'keep db')
        with self.assertRaises(ValueError):restore(self.archive,self.target)
        self.assertEqual(b'keep db',self.target.read_bytes())

    def test_target_symlink_and_stale_sidecar_are_rejected(self):
        self.make_backup();self.target.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):restore(self.archive,self.target)
        self.target.unlink()
        Path(str(self.target)+'-wal').write_bytes(b'old journal')
        with self.assertRaises(ValueError):restore(self.archive,self.target)
        self.assertFalse(self.target.exists())

    def test_foreign_project_rejected(self):
        self.make_backup()
        self.change_archive(edit_manifest=lambda m:m.update(project='pushok-foreign-bot'))
        with self.assertRaisesRegex(ValueError,'проект'):restore(self.archive,self.target)
        self.assertFalse(self.target.exists())
        metadata(self.service,PROJECT_KEY,'pushok-foreign-bot',write=True)
        with self.assertRaisesRegex(ValueError,'проект'):require_active_db(self.service.db)

    def test_checksum_mismatch_rejected(self):
        self.make_backup();self.change_archive(edit_manifest=lambda m:m.update(sha256='0'*64))
        with self.assertRaisesRegex(ValueError,'сумма'):restore(self.archive,self.target)
        self.assertFalse(self.target.exists())

    def test_wrong_manifest_counts_rejected(self):
        self.make_backup();self.change_archive(edit_manifest=lambda m:m.update(events=999))
        with self.assertRaisesRegex(ValueError,'Манифест'):verify(self.archive)

    def test_corrupt_database_rejected_even_with_matching_checksum(self):
        self.make_backup()
        self.change_archive(edit_manifest=lambda m:m.update(database_bytes=3,sha256=hashlib.sha256(b'bad').hexdigest()),edit_db=lambda _:b'bad')
        with self.assertRaises(sqlite3.DatabaseError):restore(self.archive,self.target)
        self.assertFalse(self.target.exists())

    def test_paths_extra_entries_and_large_size_rejected(self):
        self.make_backup();self.change_archive(extra='../escape')
        with self.assertRaises(ValueError):verify(self.archive)
        self.assertFalse((self.root.parent/'escape').exists())
        self.archive.unlink();self.make_backup()
        with patch('maintenance.MAX_DB_BYTES',1):
            with self.assertRaises(ValueError):verify(self.archive)

    def test_unknown_database_and_unbound_database_rejected(self):
        unknown=self.root/'other.db'
        with sqlite3.connect(unknown) as db:db.execute('CREATE TABLE other(id)')
        with self.assertRaises(ValueError):backup(unknown,self.archive)
        with self.service.db:self.service.db.execute("DELETE FROM transport_meta WHERE key='max:identity'")
        with self.assertRaises(ValueError):self.make_backup()
        self.assertFalse(self.archive.exists())

    def test_snapshot_deadline_leaves_no_published_archive(self):
        with patch('maintenance.time.monotonic',side_effect=[0,31]):
            with self.assertRaisesRegex(ValueError,'30 секунд'):self.make_backup()
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.root.glob('.pushok-backup-*')))

    def test_atomic_publish_failure_never_overwrites_existing_target(self):
        with patch('maintenance.os.link',side_effect=FileExistsError):
            with self.assertRaises(FileExistsError):self.make_backup()
        self.assertFalse(self.archive.exists())
        self.assertFalse(list(self.root.glob('.pushok-backup-*')))

    def test_activation_needs_ack_and_holds_old_queue(self):
        self.make_backup();restore(self.archive,self.target)
        with self.assertRaises(ValueError):activate(self.target)
        activate(self.target,True)
        restored=Service(str(self.target))
        try:
            require_active_db(restored.db)
            self.assertEqual('held',restored.db.execute('SELECT status FROM outbox').fetchone()[0])
            self.assertEqual('held',restored.db.execute('SELECT status FROM webhook_inbox').fetchone()[0])
            class NeverSend:
                def send(self,*args):raise AssertionError('Old queue must not send')
            self.assertFalse(worker_tick(restored,NeverSend(),Inbox(self.target),{'-100':{'admins':['42']}}))
            self.assertIsNone(restored.execute('max','-100','42','/help',True,'one'))
        finally:restored.db.close()
        with self.assertRaises(ValueError):activate(self.target,True)

    def test_activation_of_live_database_is_rejected(self):
        with self.assertRaises(ValueError):activate(self.path,True)

    def test_restore_and_activation_respect_process_lock(self):
        self.make_backup()
        with open(str(self.target)+'.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'занята'):restore(self.archive,self.target)
        restore(self.archive,self.target)
        with open(str(self.target)+'.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'занята'):activate(self.target,True)

    def test_cli_verify_is_offline_and_reports_counts_without_content(self):
        self.make_backup()
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('maintenance.py')),'verify',str(self.archive)],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        report=json.loads(result.stdout)
        self.assertTrue(report['verified']);self.assertNotIn('identities',report)

    def test_polling_and_webhook_block_restored_db_before_network(self):
        self.make_backup();restore(self.archive,self.target)
        group_file=self.root/'groups.json';group_file.write_text('{"max":{"-100":{"admins":["42"]}}}')
        env={'BOT_PLATFORM':'max','MAX_TOKEN':'test-token','DB_PATH':str(self.target),'GROUPS_FILE':str(group_file),'MAX_WEBHOOK_SECRET':'x'*40}
        import main, max_webhook
        with patch.dict(os.environ,env,clear=True),patch.object(sys,'argv',['main.py','max']),patch.object(main.Max,'start') as start:
            with self.assertRaisesRegex(ValueError,'приостановлена'):main.run()
            start.assert_not_called()
        with patch.dict(os.environ,env,clear=True),patch.object(max_webhook.Max,'api') as api:
            with self.assertRaisesRegex(ValueError,'приостановлена'):max_webhook.run_worker()
            with self.assertRaisesRegex(ValueError,'приостановлена'):max_webhook.create_app()
            api.assert_not_called()


if __name__ == '__main__':unittest.main()
