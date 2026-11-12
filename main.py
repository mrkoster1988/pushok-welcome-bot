"""Запуск одного независимого бота на выбранной платформе."""
import argparse
import fcntl
import json
import logging
import os
import time
from pathlib import Path
from core import Service
from maintenance import require_active_db
from settings import PLATFORMS, credentials, load_groups
from transports import Max, Telegram, TransportError, Vk, normalize


def metadata(service, key, value=None, *, write=False):
    if write:
        with service.db:
            service.db.execute('INSERT OR REPLACE INTO transport_meta VALUES (?,?)', (key, json.dumps(value)))
        return value
    row = service.db.execute('SELECT value FROM transport_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def bind_identity(service, platform, identity):
    service.db.execute('CREATE TABLE IF NOT EXISTS transport_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    service.db.commit()
    key = platform + ':identity'
    previous = metadata(service, key)
    if previous is not None and previous != identity:
        raise ValueError('Эта база привязана к другому боту/сообществу. Укажите отдельный DB_PATH.')
    metadata(service, key, identity, write=True)


def process_update(service, transport, groups, update):
    event = transport.normalize(update)
    if event is None or event.chat not in groups:
        return
    reply = service.execute(transport.platform, event.chat, event.user, event.text,
                            event.user in groups[event.chat]['admins'], event.id)
    if reply:
        try:
            transport.send(event, reply)
        except TransportError:
            # Не повторяем отправку с неизвестным исходом. Состояние команды уже сохранено.
            logging.error('%s: ответ не подтверждён; команда сохранена. Проверьте права и доступность API.', transport.platform)


def poll_once(service, transport, groups, cursor):
    updates, next_cursor = transport.poll(cursor)
    for update in updates:
        process_update(service, transport, groups, update)
    # При аварии раньше этой точки событие придёт снова, но Service не применит его повторно.
    metadata(service, transport.platform + ':cursor', next_cursor, write=True)
    return next_cursor


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', nargs='?', choices=['demo', 'check', *PLATFORMS],
                        default=os.environ.get('BOT_PLATFORM', 'telegram'))
    parser.add_argument('--platform', choices=PLATFORMS, help='Платформа для режима check')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    if args.mode == 'demo':
        service = Service(':memory:')
        print('Локальная демонстрация, пользователь demo — администратор. /help, /quit')
        try:
            while True:
                try:
                    text = input('> ')
                except EOFError:
                    break
                if text == '/quit':
                    break
                print(service.execute('demo', 'demo', 'demo', text, True))
        finally:
            service.db.close()
        return
    platform = (args.platform or os.environ.get('BOT_PLATFORM', 'telegram')) if args.mode == 'check' else args.mode
    if platform not in PLATFORMS:
        raise ValueError('BOT_PLATFORM должен быть telegram, max или vk.')
    groups = load_groups(os.environ.get('GROUPS_FILE', 'groups.json'), platform)
    token = credentials(platform, os.environ)
    if args.mode == 'check':
        print(f'Настройки {platform}: OK, чатов: {len(groups)}. Токен, сеть и доставка не проверялись.')
        return
    transport = {'telegram': lambda: Telegram(token),
                 'max': lambda: Max(token, os.environ.get('MAX_CA_FILE') or None),
                 'vk': lambda: Vk(token, os.environ['VK_GROUP_ID'])}[platform]()
    db_path = os.environ.get('DB_PATH') or f'data/{platform}.sqlite3'
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with open(db_path + '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Эта база уже используется другим процессом.') from None
        service = Service(db_path)
        try:
            require_active_db(service.db)
            identity = transport.start()
            bind_identity(service, platform, identity)
            cursor = metadata(service, platform + ':cursor')
            logging.info('%s polling запущен. Доставку подтвердите в тестовом чате.', platform)
            while True:
                try:
                    cursor = poll_once(service, transport, groups, cursor)
                except TransportError:
                    logging.warning('%s: получение событий не удалось; повтор через 5 секунд.', platform)
                    time.sleep(5)
        finally:
            service.db.close()


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        pass
    except (ValueError, TransportError) as exc:
        raise SystemExit(str(exc)) from None
