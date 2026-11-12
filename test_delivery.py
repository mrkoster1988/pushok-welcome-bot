"""Аварии между обработкой, курсором и отправкой; CLI работает на реальной SQLite."""
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
from core import Service
from delivery import drain,prune,recover_interrupted_sends,resolve,send_next,status
from main import bind_identity,metadata,poll_once,process_update
from maintenance import backup,restore,require_active_db
from test_transports import max_update,vk_update
from transports import Max,Telegram,TransportError,Vk


def tg_update(text='/help',uid=42,ident=1):
    return {'update_id':ident,'message':{'chat':{'id':-100,'type':'supergroup'},
            'from':{'id':uid,'is_bot':False},'text':text}}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'bot.db'
        self.service=Service(str(self.path));self.addCleanup(self.service.db.close)
        require_active_db(self.service.db)
        for platform in ('telegram','vk','max'):bind_identity(self.service,platform,'123')
        self.groups={'-100':{'admins':['42']},'2000000001':{'admins':['42']}}
        self.net=patch('socket.create_connection',side_effect=AssertionError('No live API'))
        self.net.start();self.addCleanup(self.net.stop)

    def adapters(self):
        tg=Telegram('fake');tg.username='ours'
        for adapter,make,chat in ((tg,tg_update,'-100'),(Vk('fake',123),vk_update,'2000000001'),(Max('fake'),max_update,'-100')):
            adapter.send=Mock()
            yield adapter,make,chat

    def queue(self,event_id='one',platform='telegram',state='pending',chat='-100'):
        self.service.execute(platform,chat,'42','/help',True,event_id,enqueue_reply=True)
        with self.service.db:
            self.service.db.execute('UPDATE outbox SET status=? WHERE platform=? AND id=?',(state,platform,event_id))

    def restart(self):
        self.service.db.close();self.service=Service(str(self.path));self.addCleanup(self.service.db.close)

    def cli(self,*args):
        return subprocess.run([sys.executable,str(Path(__file__).with_name('delivery.py')),'--db',str(self.path),
                               '--platform','telegram',*args],capture_output=True,text=True,timeout=10)

    def test_polling_persists_before_send_and_sends_after_cursor(self):
        for adapter,make,chat in self.adapters():
            adapter.poll=Mock(return_value=([make()],77))
            def confirm(event,text):
                with sqlite3.connect(self.path) as db:
                    self.assertEqual('sending',db.execute('SELECT status FROM outbox WHERE platform=? AND id=?',(adapter.platform,event.id)).fetchone()[0])
                    self.assertEqual('77',db.execute('SELECT value FROM transport_meta WHERE key=?',(adapter.platform+':cursor',)).fetchone()[0])
            adapter.send.side_effect=confirm
            self.assertEqual(77,poll_once(self.service,adapter,self.groups,None))
            adapter.send.assert_called_once()
            self.assertEqual({'sent':1},status(self.service.db,adapter.platform)['outbox'])

    def test_cursor_write_crash_retains_reply_and_deduplicates_command_after_restart(self):
        for adapter,make,chat in self.adapters():
            adapter.poll=Mock(return_value=([make('/faqadd Вопрос | Ответ')],88))
            with patch('main.metadata',side_effect=RuntimeError('crash before cursor')):
                with self.assertRaises(RuntimeError):poll_once(self.service,adapter,self.groups,None)
            adapter.send.assert_not_called()
            self.assertEqual({'pending':1},status(self.service.db,adapter.platform)['outbox'])
            self.restart()
            poll_once(self.service,adapter,self.groups,None)
            adapter.send.assert_called_once()
            data=json.loads(self.service.db.execute('SELECT data FROM chats WHERE scope=?',(json.dumps([adapter.platform,chat]),)).fetchone()[0])
            self.assertEqual(1,len(data['faq']))

    def test_drain_precedes_failed_poll(self):
        self.queue();adapter=Mock(platform='telegram');adapter.poll.side_effect=TransportError('no network')
        with self.assertRaises(TransportError):poll_once(self.service,adapter,self.groups,None)
        adapter.send.assert_called_once()
        self.assertEqual({'sent':1},status(self.service.db,'telegram')['outbox'])

    def test_crash_before_send_stays_pending_and_is_sent_after_restart(self):
        self.queue();self.restart()
        adapter=Mock(platform='telegram')
        recover_interrupted_sends(self.service,'telegram')
        drain(self.service,adapter,self.groups)
        adapter.send.assert_called_once()

    def test_crash_during_send_recovers_as_uncertain_without_resend(self):
        for adapter,make,chat in self.adapters():
            self.queue(platform=adapter.platform,chat=chat)
            adapter.send.side_effect=KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):send_next(self.service,adapter,self.groups)
            self.restart();recover_interrupted_sends(self.service,adapter.platform)
            self.assertEqual({'uncertain':1},status(self.service.db,adapter.platform)['outbox'])
            drain(self.service,adapter,self.groups)
            adapter.send.assert_called_once()

    def test_exception_has_no_secret_in_logs_and_does_not_repeat(self):
        self.queue();adapter=Mock(platform='telegram')
        adapter.send.side_effect=RuntimeError('PRIVATE_TOKEN_AND_TEXT')
        with self.assertLogs(level='ERROR') as logs:drain(self.service,adapter,self.groups)
        self.assertNotIn('PRIVATE_TOKEN_AND_TEXT',str(logs.output))
        drain(self.service,adapter,self.groups);adapter.send.assert_called_once()

    def test_recovery_is_scoped_to_platform(self):
        self.queue(platform='telegram',state='sending');self.queue(platform='vk',state='sending')
        recover_interrupted_sends(self.service,'telegram')
        self.assertEqual({'uncertain':1},status(self.service.db,'telegram')['outbox'])
        self.assertEqual({'sending':1},status(self.service.db,'vk')['outbox'])

    def test_failed_send_stops_batch_and_preserves_later_pending_replies(self):
        self.queue('first');self.queue('second');self.queue('third')
        adapter=Mock(platform='telegram');adapter.send.side_effect=TransportError('unavailable')
        with self.assertLogs(level='ERROR'):
            self.assertEqual(1,drain(self.service,adapter,self.groups))
        adapter.send.assert_called_once()
        self.assertEqual({'pending':2,'uncertain':1},status(self.service.db,'telegram')['outbox'])

    def test_removed_chat_is_cancelled_and_other_platform_untouched(self):
        self.queue();self.queue(platform='vk')
        adapter=Mock(platform='telegram');drain(self.service,adapter,{})
        adapter.send.assert_not_called()
        self.assertEqual({'cancelled':1},status(self.service.db,'telegram')['outbox'])
        self.assertEqual({'pending':1},status(self.service.db,'vk')['outbox'])

    def test_drain_has_bounded_batch_and_preserves_order(self):
        for i in range(4):self.queue(str(i))
        adapter=Mock(platform='telegram')
        self.assertEqual(2,drain(self.service,adapter,self.groups,limit=2))
        self.assertEqual(['0','1'],[c.args[0].id for c in adapter.send.call_args_list])
        self.assertEqual({'pending':2,'sent':2},status(self.service.db,'telegram')['outbox'])

    def test_queue_insert_failure_rolls_back_command_event_and_quota(self):
        self.service.db.execute("CREATE TRIGGER block_reply BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.execute('telegram','-100','99','/help',False,'atomic',enqueue_reply=True)
        self.assertEqual(0,self.service.db.execute('SELECT count(*) FROM events').fetchone()[0])
        self.assertEqual(0,self.service.db.execute('SELECT count(*) FROM chats').fetchone()[0])
        self.assertEqual(0,self.service.db.execute("SELECT count(*) FROM transport_meta WHERE key LIKE 'guard:%'").fetchone()[0])

    def test_partial_batch_replay_does_not_apply_first_command_twice(self):
        adapter=Telegram('fake');adapter.username='ours';adapter.send=Mock()
        updates=[tg_update('/faqadd Один | Ответ',ident=1),tg_update('/faqadd Два | Ответ',ident=2)]
        adapter.poll=Mock(return_value=(updates,3));execute=self.service.execute
        def fail_second(*args,**kwargs):
            if args[5]=='2':raise RuntimeError('crash')
            return execute(*args,**kwargs)
        with patch.object(self.service,'execute',side_effect=fail_second):
            with self.assertRaises(RuntimeError):poll_once(self.service,adapter,self.groups,None)
        self.assertIsNone(metadata(self.service,'telegram:cursor'))
        self.restart();poll_once(self.service,adapter,self.groups,None)
        self.assertEqual(2,adapter.send.call_count)
        self.assertIn('Два',self.service.execute('telegram','-100','42','/answer 2',True))

    def test_status_is_readonly_and_omits_reply_text(self):
        self.queue(state='uncertain')
        before=list(self.service.db.iterdump());result=self.cli('status','--details')
        self.assertEqual(0,result.returncode,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual({'uncertain':1},data['outbox'])
        self.assertEqual('one',data['unresolved'][0]['event_id'])
        self.assertNotIn('reply',result.stdout)
        self.assertEqual(before,list(self.service.db.iterdump()))

    def test_resolve_preview_then_apply_never_sends(self):
        self.queue(state='uncertain')
        preview=self.cli('resolve','--event-id','one','--as','sent')
        self.assertEqual(0,preview.returncode,preview.stderr)
        self.assertFalse(json.loads(preview.stdout)['applied'])
        self.assertEqual({'uncertain':1},status(self.service.db,'telegram')['outbox'])
        applied=self.cli('resolve','--event-id','one','--as','sent','--apply')
        self.assertEqual(0,applied.returncode,applied.stderr)
        adapter=Mock(platform='telegram');drain(self.service,adapter,self.groups)
        adapter.send.assert_not_called()
        self.assertEqual({'sent':1},status(self.service.db,'telegram')['outbox'])

    def test_resolve_requires_uncertain_and_correct_platform(self):
        for state in ('pending','sending','sent','held','cancelled'):
            self.queue(state,state=state)
            with self.assertRaises(ValueError):resolve(self.service.db,'telegram',state,'sent',True)
        self.queue('u',state='uncertain')
        with self.assertRaises(ValueError):resolve(self.service.db,'vk','u','sent',True)
        with self.assertRaises(ValueError):resolve(self.service.db,'telegram','u','pending',True)

    def test_cli_apply_refuses_running_worker(self):
        self.queue(state='uncertain')
        with open(str(self.path)+'.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            result=self.cli('resolve','--event-id','one','--as','cancelled','--apply')
        self.assertNotEqual(0,result.returncode)
        self.assertIn('занята',result.stderr)
        self.assertEqual({'uncertain':1},status(self.service.db,'telegram')['outbox'])

    def test_cli_missing_database_does_not_create_it(self):
        self.service.db.close();self.path.unlink()
        result=self.cli('status')
        self.assertNotEqual(0,result.returncode);self.assertFalse(self.path.exists())

    def test_cli_refuses_wrong_project_and_restore_hold(self):
        self.queue(state='uncertain')
        archive=Path(self.temp.name)/'backup.zip';target=Path(self.temp.name)/'restored.db'
        backup(self.path,archive);restore(archive,target)
        original=self.path;self.path=target
        result=self.cli('resolve','--event-id','one','--as','sent','--apply')
        self.assertNotEqual(0,result.returncode);self.assertIn('приостановлена',result.stderr)
        self.path=original
        metadata(self.service,'maintenance:project','pushok-other-bot',write=True)
        result=self.cli('status');self.assertNotEqual(0,result.returncode)

    def test_prune_keeps_unresolved_other_platform_and_event_deduplication(self):
        for i,state in enumerate(('sent','cancelled','sent','pending','sending','uncertain','held')):
            self.queue(str(i),state=state)
        self.queue('vk',platform='vk',state='sent')
        before=list(self.service.db.iterdump())
        self.assertEqual(2,prune(self.service.db,'telegram',1)['eligible'])
        self.assertEqual(before,list(self.service.db.iterdump()))
        with self.service.db:prune(self.service.db,'telegram',1,True)
        self.assertEqual(8,self.service.db.execute('SELECT count(*) FROM events').fetchone()[0])
        self.assertEqual(6,self.service.db.execute('SELECT count(*) FROM outbox').fetchone()[0])
        self.assertIsNone(self.service.execute('telegram','-100','42','/help',True,'0',enqueue_reply=True))
        self.assertEqual(6,self.service.db.execute('SELECT count(*) FROM outbox').fetchone()[0])

    def test_prune_cli_preview_apply_and_invalid_keep(self):
        self.queue(state='sent')
        preview=self.cli('prune','--keep-last','0')
        self.assertEqual(0,preview.returncode,preview.stderr)
        self.assertEqual(1,json.loads(preview.stdout)['eligible'])
        invalid=self.cli('prune','--keep-last','-1','--apply')
        self.assertNotEqual(0,invalid.returncode)
        result=self.cli('prune','--keep-last','0','--apply')
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual({},status(self.service.db,'telegram')['outbox'])


if __name__ == '__main__':unittest.main()
