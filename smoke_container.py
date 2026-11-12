"""CI: проверка установленного образа без сети, реальных токенов и отправки сообщений."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from core import Service
from main import bind_identity
from maintenance import activate,backup,project_id,require_active_db,restore,verify

assert os.getuid() == 10001, 'Контейнер должен работать от пользователя bot'
assert project_id().startswith('pushok-')
assert Path('VERSION').read_text().strip()
if len(sys.argv)>1 and sys.argv[1]=='webhook':
    import max_webhook
with tempfile.TemporaryDirectory(dir='data') as folder:
    root=Path(folder); source=root/'source.sqlite3'; target=root/'restored.sqlite3'; archive=root/'backup.zip'
    service=Service(str(source))
    try:
        bind_identity(service,'max','123')
        require_active_db(service.db)
        service.execute('max','-100','42','/help',True,'smoke',enqueue_reply=True)
        service.execute('max','-100','42','/faqadd Где правила? | /rules',True)
        assert '/rules' in service.execute('max','-100','42','/answer 1')
        service.execute('max','-100','42','/welcomeon',True)
        greeting=service.execute('max','-100','43','',event_id='smoke-join',enqueue_reply=True,kind='join')
        assert 'Добро пожаловать' in greeting
        assert service.execute('max','-100','43','',event_id='smoke-join',kind='join') is None
        backup(source,archive)
        assert verify(archive)['verified']
        restore(archive,target)
        with sqlite3.connect(target) as db:
            try:require_active_db(db)
            except ValueError:pass
            else:raise AssertionError('Восстановленная база должна блокировать запуск')
        activate(target,True)
        with sqlite3.connect(target) as db:
            require_active_db(db)
            assert db.execute('SELECT status FROM outbox').fetchall()==[('held',),('held',)]
    finally:service.db.close()
print('Container smoke OK: non-root, FAQ, welcome, deduplication, backup/restore; network disabled')
