"""Локальная демонстрация и Telegram long polling, без внешних зависимостей."""
import argparse
import fcntl
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from core import Service


def api(token, method, payload):
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=40) as response:
        data = json.load(response)
    if not data.get('ok'):
        raise RuntimeError('Telegram API отклонил запрос')
    return data['result']


def normalize(update, username):
    message = update.get('message', {})
    author = message.get('from', {})
    if author.get('is_bot') or message.get('sender_chat') or not author.get('id'):
        return None  # Анонимный администратор не получает полномочия по имени.
    text = message.get('text', '')
    if not text.startswith('/'):
        return None
    command, _, arg = text.partition(' ')
    if '@' in command:
        command, target = command.split('@', 1)
        if target.lower() != username.lower():
            return None
    return str(message['chat']['id']), str(author['id']), command + (' ' + arg if arg else '')


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['demo', 'telegram'])
    args = parser.parse_args()
    if args.mode == 'demo':
        service = Service(':memory:')
        print('Локальная демонстрация, пользователь demo — администратор. /help, /quit')
        while True:
            try:
                text = input('> ')
            except EOFError:
                break
            if text == '/quit':
                break
            print(service.execute('demo', 'demo', 'demo', text, True))
        return
    token = os.environ.get('TELEGRAM_TOKEN', '').strip()
    if not token:
        raise SystemExit('Задайте TELEGRAM_TOKEN в окружении. Не сохраняйте его в Git.')
    groups = json.loads(Path(os.environ.get('GROUPS_FILE', 'groups.json')).read_text())
    if not isinstance(groups, dict) or not groups:
        raise SystemExit('Нужен непустой объект разрешённых чатов в groups.json')
    for chat, config in groups.items():
        if not isinstance(config, dict) or not isinstance(config.get('admins'), list):
            raise SystemExit('В каждом чате groups.json нужен массив admins')
        config['admins'] = [str(x) for x in config['admins']]
    db_path = os.environ.get('DB_PATH', 'data/bot.sqlite3')
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # Один процесс на локальную базу; сетевые диски и несколько серверов не поддерживаются.
    lock = open(db_path + '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Эта база уже используется другим процессом')
    service = Service(db_path)
    try:
        me = api(token, 'getMe', {})
        hook = api(token, 'getWebhookInfo', {})
    except Exception:
        raise SystemExit('Не удалось проверить Telegram: проверьте токен и сеть.') from None
    if hook.get('url'):
        raise SystemExit('У бота уже настроен webhook. Используйте отдельного бота с пустым webhook.')
    offset = 0
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    logging.info('Telegram polling запущен; это ещё не подтверждение доставки.')
    while True:
        try:
            updates = api(token, 'getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message']})
        except Exception:
            logging.warning('Получение событий не удалось; повтор через 5 секунд.')
            time.sleep(5)
            continue
        for update in updates:
            normalized = normalize(update, me['username'])
            if normalized:
                chat, user, text = normalized
                if chat in groups:
                    reply = service.execute('telegram', chat, user, text, user in groups[chat]['admins'], update['update_id'])
                    if reply:
                        try:
                            api(token, 'sendMessage', {'chat_id': chat, 'text': reply})
                        except Exception:
                            # Не повторяем неизвестный исход отправки, чтобы не плодить сообщения.
                            logging.error('Ответ не подтверждён: update_id=%s. Команда уже сохранена.', update['update_id'])
            offset = update['update_id'] + 1


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        pass
