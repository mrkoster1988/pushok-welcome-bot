"""Резервная копия SQLite и восстановление в новый файл, без сетевых запросов."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

HOLD_KEY = 'maintenance:restore_hold'
PROJECT_KEY = 'maintenance:project'
MAX_DB_BYTES = 1024 * 1024 * 1024
MEMBERS = {'manifest.json', 'database.sqlite3'}


def project_id():
    value = Path(__file__).with_name('PROJECT_ID').read_text().strip()
    if not re.fullmatch(r'pushok-[a-z]+-bot', value):
        raise ValueError('Некорректный PROJECT_ID рядом с maintenance.py.')
    return value


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def meta(db, key):
    if 'transport_meta' not in tables(db):
        return None
    row = db.execute('SELECT value FROM transport_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def require_active_db(db):
    """Проверяется до API: восстановленная база не запускается без явной активации."""
    if meta(db, HOLD_KEY):
        raise ValueError('База восстановлена и приостановлена. См. BACKUP.md и maintenance.py activate.')
    previous = meta(db, PROJECT_KEY)
    if previous is not None and previous != project_id():
        raise ValueError('База относится к другому проекту Pushok.')
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS transport_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO transport_meta VALUES (?,?)', (PROJECT_KEY,json.dumps(project_id())))


def readonly(path):
    path = Path(path).absolute()
    if not path.is_file() or path.is_symlink():
        raise ValueError('Источник должен быть существующим обычным файлом, не ссылкой.')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    db.execute('PRAGMA query_only=ON')
    db.execute('PRAGMA trusted_schema=OFF')
    return db


def inspect_db(db):
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('Проверка целостности SQLite не пройдена.')
    existing = tables(db)
    required = {'chats': {'scope','data'}, 'events': {'platform','id'},
                'transport_meta': {'key','value'}}
    optional = {'outbox': {'platform','id','chat','user','reply','status'},
                'webhook_inbox': {'id','event','status','attempts','received_at'}}
    if not set(required) <= existing:
        raise ValueError('Нужна база запущенного бота Pushok alpha.2 или новее.')
    if existing - set(required) - set(optional) or db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
        raise ValueError('Структура базы не поддерживается этой версией.')
    for name, columns in {**required, **optional}.items():
        if name in existing and {r[1] for r in db.execute(f'PRAGMA table_info({name})')} != columns:
            raise ValueError('Структура таблиц не соответствует формату Pushok.')
    identities = {k: json.loads(v) for k,v in db.execute("SELECT key,value FROM transport_meta WHERE key IN ('telegram:identity','max:identity','vk:identity')")}
    if not identities or any(not isinstance(v,str) or not re.fullmatch(r'[1-9][0-9]*',v) for v in identities.values()):
        raise ValueError('В базе нет корректной привязки к боту/сообществу.')
    return {'identities':identities,'chats':db.execute('SELECT count(*) FROM chats').fetchone()[0],
            'events':db.execute('SELECT count(*) FROM events').fetchone()[0]}


def digest(path):
    with open(path,'rb') as source:
        return hashlib.file_digest(source,'sha256').hexdigest()


def ensure_new(path):
    for suffix in ('','-wal','-shm','-journal'):
        if os.path.lexists(str(path)+suffix):
            raise ValueError('Файл назначения или его журнал уже существует. Выберите новое имя.')


def publish(source, target):
    # Одна файловая система; hard link создаёт окончательное имя без перезаписи при гонке.
    os.chmod(source,0o600)
    with open(source,'rb') as handle:
        os.fsync(handle.fileno())
    os.link(source,target)
    directory = os.open(str(Path(target).parent),os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def snapshot(source, target, timeout=30):
    deadline = time.monotonic() + timeout
    def progress(status,remaining,total):
        if time.monotonic() > deadline:
            raise ValueError('Не удалось закончить копирование за 30 секунд; снимок не опубликован.')
    with closing(readonly(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst,pages=256,progress=progress,sleep=0.05)
        dst.execute('PRAGMA journal_mode=DELETE')
        return inspect_db(dst)


def backup(source, output):
    output = Path(output).absolute()
    ensure_new(output)
    output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='.pushok-backup-',dir=output.parent) as temp:
        db_path = Path(temp)/'database.sqlite3'
        report = snapshot(source,db_path)
        with closing(readonly(db_path)) as db:
            previous = meta(db,PROJECT_KEY)
            if previous is not None and previous != project_id():
                raise ValueError('База относится к другому проекту Pushok.')
        size = db_path.stat().st_size
        if size > MAX_DB_BYTES:
            raise ValueError('В этом выпуске поддерживается база до 1 ГиБ.')
        manifest = {'format':1,'project':project_id(),
                    'version':Path(__file__).with_name('VERSION').read_text().strip(),
                    'created_at':datetime.now(timezone.utc).isoformat(),
                    'database_bytes':size,'sha256':digest(db_path),**report}
        archive = Path(temp)/'backup.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
            z.write(db_path,'database.sqlite3')
            z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        publish(archive,output)
    return {'project':manifest['project'],'chats':report['chats'],'events':report['events']}


def unpack(archive, folder):
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) != 2 or set(z.namelist()) != MEMBERS:
            raise ValueError('Архив должен содержать только manifest.json и database.sqlite3.')
        if z.getinfo('manifest.json').file_size > 16384:
            raise ValueError('Манифест слишком большой.')
        manifest = json.loads(z.read('manifest.json'))
        if not isinstance(manifest,dict) or manifest.get('format') != 1 or manifest.get('project') != project_id():
            raise ValueError('Неверный формат архива или другой проект Pushok.')
        info = z.getinfo('database.sqlite3')
        if not 0 < info.file_size <= MAX_DB_BYTES or manifest.get('database_bytes') != info.file_size:
            raise ValueError('Размер базы не соответствует манифесту или превышает 1 ГиБ.')
        target = Path(folder)/'database.sqlite3'
        # Никаких extract/extractall: имена из архива не становятся путями на сервере.
        with z.open(info) as src, target.open('xb') as dst:
            written = 0
            while chunk := src.read(1024*1024):
                written += len(chunk)
                if written > info.file_size:
                    raise ValueError('Размер распакованной базы превышен.')
                dst.write(chunk)
        os.chmod(target,0o600)
    if digest(target) != manifest.get('sha256'):
        raise ValueError('Контрольная сумма базы не совпала.')
    with closing(readonly(target)) as db:
        report = inspect_db(db)
        previous = meta(db,PROJECT_KEY)
    if previous is not None and previous != project_id():
        raise ValueError('База внутри архива относится к другому проекту.')
    if any(manifest.get(k) != v for k,v in report.items()):
        raise ValueError('Манифест не соответствует содержимому базы.')
    return target,report


def verify(archive):
    with tempfile.TemporaryDirectory(prefix='pushok-verify-') as temp:
        _,report = unpack(archive,temp)
    return {'project':project_id(),'chats':report['chats'],'events':report['events'],'verified':True}


@contextmanager
def lock_database(path):
    fd = os.open(str(path)+'.lock',os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('База занята процессом бота. Сначала остановите его.') from None
        yield


def restore(archive, target):
    target = Path(target).absolute()
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with lock_database(target):
        ensure_new(target)
        with tempfile.TemporaryDirectory(prefix='.pushok-restore-',dir=target.parent) as temp:
            path,report = unpack(archive,temp)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('INSERT OR REPLACE INTO transport_meta VALUES (?,?)',(HOLD_KEY,'true'))
                db.execute('INSERT OR REPLACE INTO transport_meta VALUES (?,?)',(PROJECT_KEY,json.dumps(project_id())))
                db.execute("DELETE FROM transport_meta WHERE key LIKE '%:webhook_heartbeat'")
            publish(path,target)
    return {'project':project_id(),'chats':report['chats'],'events':report['events'],'delivery':'held'}


def activate(path, acknowledge=False):
    if not acknowledge:
        raise ValueError('Активация требует --acknowledge-rollback; сначала прочитайте BACKUP.md.')
    path = Path(path).absolute()
    with lock_database(path), closing(readonly(path)) as check:
        inspect_db(check)
        if meta(check,PROJECT_KEY) != project_id() or meta(check,HOLD_KEY) is not True:
            raise ValueError('Ожидалась приостановленная восстановленная база этого проекта.')
        with closing(sqlite3.connect(path)) as db, db:
            # Старая очередь не отправляется сама при запуске: после снимка эти ответы уже могли уйти.
            if 'outbox' in tables(db):
                db.execute("UPDATE outbox SET status='held' WHERE status IN ('pending','sending','uncertain')")
            if 'webhook_inbox' in tables(db):
                db.execute("UPDATE webhook_inbox SET status='held' WHERE status='pending'")
            db.execute('DELETE FROM transport_meta WHERE key=?',(HOLD_KEY,))
    return {'activated':True,'old_queue':'held','rollback_risk_acknowledged':True}


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode',required=True)
    p = sub.add_parser('backup'); p.add_argument('--db',required=True); p.add_argument('--output',required=True)
    p = sub.add_parser('verify'); p.add_argument('archive')
    p = sub.add_parser('restore'); p.add_argument('archive'); p.add_argument('--to',required=True)
    p = sub.add_parser('activate'); p.add_argument('--db',required=True); p.add_argument('--acknowledge-rollback',action='store_true')
    args = parser.parse_args()
    if args.mode == 'backup': result = backup(args.db,args.output)
    elif args.mode == 'verify': result = verify(args.archive)
    elif args.mode == 'restore': result = restore(args.archive,args.to)
    else: result = activate(args.db,args.acknowledge_rollback)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        cli()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError,sqlite3.Error,zipfile.BadZipFile,RuntimeError):
        raise SystemExit('Операция не завершена: проверьте путь, права, свободное место и формат файла.') from None
