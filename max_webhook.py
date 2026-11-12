"""MAX webhook: защищённый WSGI-приёмник и отдельный обработчик SQLite-очереди."""
import argparse
import fcntl
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from core import Service
from maintenance import require_active_db
from main import bind_identity, metadata
from settings import credentials, load_groups
from transports import Event, Max, TransportError, numeric_id

MAX_BODY = 65536
WEBHOOK_PATH = '/webhooks/max'


def secret_from_env(env):
    value = env.get('MAX_WEBHOOK_SECRET', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value):
        raise ValueError('MAX_WEBHOOK_SECRET: нужны 32–256 символов A-Z, a-z, 0-9, _ или -.')
    return value


def db_path(env):
    return env.get('DB_PATH') or 'data/max.sqlite3'


def transport_from_env(env):
    return Max(credentials('max', env), env.get('MAX_CA_FILE') or None)


class Inbox:
    def __init__(self, path, limit=10000):
        if limit < 1:
            raise ValueError('Лимит очереди должен быть положительным.')
        self.path, self.limit = str(path), limit
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS webhook_inbox (
                id TEXT PRIMARY KEY, event TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, received_at INTEGER NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS webhook_pending ON webhook_inbox(status,received_at)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def enqueue(self, event):
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM webhook_inbox WHERE id=?', (event.id,)).fetchone():
                return 'duplicate'
            pending = db.execute("SELECT count(*) FROM webhook_inbox WHERE status!='done'").fetchone()[0]
            replies = db.execute("SELECT count(*) FROM outbox WHERE platform='max' AND status='pending'").fetchone()[0]
            if pending + replies >= self.limit:
                return 'full'
            db.execute('INSERT INTO webhook_inbox(id,event,received_at) VALUES (?,?,?)',
                       (event.id, json.dumps(asdict(event), ensure_ascii=False), int(time.time())))
        return 'accepted'

    def status(self):
        with self.connect() as db:
            counts = dict(db.execute('SELECT status,count(*) FROM webhook_inbox GROUP BY status'))
            replies = dict(db.execute("SELECT status,count(*) FROM outbox WHERE platform='max' GROUP BY status"))
            row = db.execute("SELECT value FROM transport_meta WHERE key='max:webhook_heartbeat'").fetchone()
        heartbeat = json.loads(row[0]) if row else 0
        return {'worker_alive': 0 <= time.time() - heartbeat < 90, 'inbox': counts, 'outbox': replies}


class WebhookApp:
    def __init__(self, inbox, groups, secret, username=None):
        self.inbox, self.groups = inbox, groups
        self.secret = secret_from_env({'MAX_WEBHOOK_SECRET': secret})
        self.normalizer = Max('unused')
        self.normalizer.username = username

    @staticmethod
    def respond(start_response, status, body):
        payload = json.dumps(body).encode()
        start_response(status, [('Content-Type', 'application/json'), ('Content-Length', str(len(payload))),
                                ('Cache-Control', 'no-store')])
        return [payload]

    def __call__(self, environ, start_response):
        respond = lambda status, body: self.respond(start_response, status, body)
        path, method = environ.get('PATH_INFO'), environ.get('REQUEST_METHOD')
        if path == '/healthz' and method == 'GET':
            try:
                alive = self.inbox.status()['worker_alive']
            except (sqlite3.Error, ValueError, TypeError):
                alive = False
            return respond('200 OK' if alive else '503 Service Unavailable', {'ready': alive})
        if path != WEBHOOK_PATH:
            return respond('404 Not Found', {'error': 'not_found'})
        if method != 'POST':
            return respond('405 Method Not Allowed', {'error': 'post_required'})
        provided = environ.get('HTTP_X_MAX_BOT_API_SECRET', '')
        if not isinstance(provided, str) or not hmac.compare_digest(provided.encode(), self.secret.encode()):
            return respond('403 Forbidden', {'error': 'forbidden'})
        if environ.get('CONTENT_TYPE', '').split(';')[0].strip().lower() != 'application/json':
            return respond('415 Unsupported Media Type', {'error': 'json_required'})
        length = environ.get('CONTENT_LENGTH', '')
        if not re.fullmatch(r'[0-9]{1,9}', length or ''):
            return respond('411 Length Required', {'error': 'length_required'})
        length = int(length)
        if length > MAX_BODY:
            return respond('413 Content Too Large', {'error': 'body_too_large'})
        try:
            body = environ['wsgi.input'].read(length)
            if len(body) != length:
                raise ValueError('truncated')
            update = json.loads(body)
            if not isinstance(update, dict) or not isinstance(update.get('update_type'), str):
                raise ValueError('invalid_update')
            event = self.normalizer.normalize(update)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            return respond('400 Bad Request', {'error': 'invalid_update'})
        if event is None or event.chat not in self.groups:
            return respond('200 OK', {'ok': True})
        if len(event.id) > 256:
            return respond('400 Bad Request', {'error': 'invalid_event_id'})
        try:
            result = self.inbox.enqueue(event)
        except sqlite3.Error:
            logging.error('Webhook: событие не записано в очередь; секрет и содержимое скрыты.')
            return respond('503 Service Unavailable', {'error': 'storage_unavailable'})
        if result == 'full':
            return respond('503 Service Unavailable', {'error': 'queue_full'})
        # Ответ 200 отправляется только после успешного COMMIT входящего события.
        return respond('200 OK', {'ok': True})


def create_app():
    """Фабрика Gunicorn. API читается при старте; токен в журналах не выводится."""
    groups = load_groups(os.environ.get('GROUPS_FILE', 'groups.json'), 'max')
    secret = secret_from_env(os.environ)
    transport = transport_from_env(os.environ)
    inbox = Inbox(db_path(os.environ))
    with inbox.connect() as db:
        require_active_db(db)
    me = transport.api('me')
    identity = numeric_id(me.get('user_id'), positive=True)
    if not identity:
        raise ValueError('MAX не вернул идентификатор бота.')
    with inbox.connect() as db:
        row = db.execute("SELECT value FROM transport_meta WHERE key='max:identity'").fetchone()
    if row is None or json.loads(row[0]) != identity:
        raise ValueError('Сначала запустите worker с тем же ботом и базой.')
    return WebhookApp(inbox, groups, secret, me.get('username'))


def recover_interrupted_sends(service):
    with service.db:
        service.db.execute("UPDATE outbox SET status='uncertain' WHERE platform='max' AND status='sending'")


def worker_tick(service, transport, inbox, groups):
    metadata(service, 'max:webhook_heartbeat', time.time(), write=True)
    row = service.db.execute("SELECT id,event,attempts FROM webhook_inbox WHERE status='pending' ORDER BY received_at,rowid LIMIT 1").fetchone()
    if row:
        event_id, payload, attempts = row
        try:
            event = Event(**json.loads(payload))
            if event.chat in groups:
                service.execute('max', event.chat, event.user, event.text,
                                event.user in groups[event.chat]['admins'], event.id, enqueue_reply=True)
            with service.db:
                service.db.execute("UPDATE webhook_inbox SET status='done',event='{}' WHERE id=?", (event_id,))
        except Exception:
            # После трёх ошибок сохраняем запись для разбора, не блокируя все следующие события.
            with service.db:
                service.db.execute('UPDATE webhook_inbox SET attempts=attempts+1,status=? WHERE id=?',
                                   ('failed' if attempts >= 2 else 'pending', event_id))
            logging.error('MAX worker: ошибка обработки; детали события скрыты. Проверьте status.')
    reply = service.db.execute("SELECT id,chat,user,reply FROM outbox WHERE platform='max' AND status='pending' ORDER BY rowid LIMIT 1").fetchone()
    if reply:
        event_id, chat, user, text = reply
        # При удалении чата из allowlist отложенные ответы туда больше не уходят.
        state = 'sending' if chat in groups else 'cancelled'
        with service.db:
            service.db.execute("UPDATE outbox SET status=? WHERE platform='max' AND id=?", (state,event_id))
        if state == 'sending':
            try:
                transport.send(Event(chat,user,'',event_id),text)
                state = 'sent'
            except Exception:
                state = 'uncertain'
                logging.error('MAX: отправка не подтверждена. Автоматический повтор отключён; проверьте status.')
            with service.db:
                service.db.execute("UPDATE outbox SET status=? WHERE platform='max' AND id=?", (state,event_id))
    metadata(service, 'max:webhook_heartbeat', time.time(), write=True)
    return row is not None or reply is not None


def run_worker():
    groups = load_groups(os.environ.get('GROUPS_FILE', 'groups.json'), 'max')
    secret_from_env(os.environ)
    transport = transport_from_env(os.environ)
    path = db_path(os.environ)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path + '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('База уже занята: остановите polling или второй worker.') from None
        service = Service(path)
        try:
            require_active_db(service.db)
            me = transport.api('me')
            identity = numeric_id(me.get('user_id'), positive=True)
            if not identity:
                raise ValueError('MAX не вернул идентификатор бота.')
            bind_identity(service, 'max', identity)
            inbox = Inbox(path)
            recover_interrupted_sends(service)
            logging.info('MAX webhook worker запущен; реальную доставку подтвердите в тестовом чате.')
            while True:
                busy = worker_tick(service, transport, inbox, groups)
                time.sleep(0.05 if busy else 1)
        finally:
            service.db.close()


def register(transport, url, secret):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or '.' not in parsed.hostname or
            parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment or
            parsed.path != WEBHOOK_PATH):
        raise ValueError('Нужен https://ваш-домен/webhooks/max без порта, пароля и query-параметров.')
    secret_from_env({'MAX_WEBHOOK_SECRET': secret})
    existing = transport.api('subscriptions').get('subscriptions')
    if not isinstance(existing, list):
        raise ValueError('Не удалось проверить существующие подписки.')
    if any(row.get('url') != url for row in existing):
        raise ValueError('У бота уже есть подписка на другой адрес. Она не изменена; используйте отдельного бота.')
    result = transport.api('subscriptions',method='POST',body={'url':url,'update_types':['message_created'],'secret':secret})
    if result.get('success') is not True:
        raise TransportError('MAX не подтвердил регистрацию webhook.')


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check','worker','status','health','register'])
    parser.add_argument('--url')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.mode == 'worker':
        run_worker()
    elif args.mode == 'check':
        load_groups(os.environ.get('GROUPS_FILE','groups.json'),'max')
        secret_from_env(os.environ)
        credentials('max',os.environ)
        print('MAX webhook: локальные настройки OK. Сеть, TLS и доставка не проверены.')
    elif args.mode == 'register':
        if not args.url:
            raise ValueError('Укажите --url после настройки HTTPS и запуска приёмника.')
        register(transport_from_env(os.environ), args.url, secret_from_env(os.environ))
        print('MAX подтвердил подписку. Отправьте новую команду в тестовый чат и проверьте ответ.')
    else:
        if not Path(db_path(os.environ)).is_file():
            raise ValueError('База не создана; сначала запустите worker.')
        report = Inbox(db_path(os.environ)).status()
        if args.mode == 'health':
            raise SystemExit(0 if report['worker_alive'] else 1)
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        cli()
    except KeyboardInterrupt:
        pass
    except (ValueError, TransportError, sqlite3.Error):
        logging.error('Команда не выполнена. Проверьте настройки, доступ к базе и API. Секреты не выводятся.')
        raise SystemExit(1) from None
