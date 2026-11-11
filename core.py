"""Общее для этого самостоятельного бота: транзакции и изоляция чатов."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from domain import handle, HELP


class UserError(Exception):
    pass


def require_admin(admin):
    if not admin:
        raise ValueError('Нужны права администратора из groups.json.')


def parts(text, count):
    values = [x.strip() for x in text.split('|')]
    if len(values) != count or not all(values):
        raise ValueError('Неверные параметры. Используйте /help; разделитель: |')
    return values


def positive(text):
    value = int(text)
    if value < 1 or value > 1000000:
        raise ValueError('Нужно целое число от 1 до 1000000.')
    return value


def add(state, **values):
    key = str(state.setdefault('next_id', 1))
    state['next_id'] = int(key) + 1
    state.setdefault('items', {})[key] = values
    return key


def item(state, key):
    if key not in state.get('items', {}):
        raise ValueError('Запись не найдена в этом чате.')
    return state['items'][key]


def listing(state, predicate=lambda x: True):
    rows = [f"#{key}: {row.get('title', '')} [{row.get('status', 'open')}]"
            for key, row in state.get('items', {}).items() if predicate(row)]
    return '\n'.join(rows[-20:]) or 'Записей пока нет.'


class Service:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS chats (scope TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (platform TEXT, id TEXT, PRIMARY KEY(platform,id))')
        self.db.execute('''CREATE TABLE IF NOT EXISTS outbox (
            platform TEXT, id TEXT, chat TEXT NOT NULL, user TEXT NOT NULL,
            reply TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY(platform,id))''')
        self.db.commit()

    def execute(self, platform, chat, user, text, admin=False, event_id=None, enqueue_reply=False):
        if platform not in ('telegram', 'max', 'vk', 'demo'):
            raise ValueError('Неизвестная платформа')
        if enqueue_reply and event_id is None:
            raise ValueError('Для очереди ответа нужен ID события.')
        scope = json.dumps([platform, str(chat)])
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if event_id is not None and self.db.execute('SELECT 1 FROM events WHERE platform=? AND id=?', (platform, str(event_id))).fetchone():
                return None
            row = self.db.execute('SELECT data FROM chats WHERE scope=?', (scope,)).fetchone()
            state = json.loads(row[0]) if row else {}
            try:
                if not isinstance(text, str) or len(text) > 3000:
                    raise ValueError('Команда должна быть не длиннее 3000 символов.')
                cmd, _, arg = text.strip().partition(' ')
                result = HELP if cmd in ('/start', '/help') else handle(state, str(user), cmd.lower(), arg.strip(), admin)
                if result is None:
                    result = 'Неизвестная команда. /help'
            except (ValueError, KeyError, IndexError) as exc:
                # Частично изменённое состояние не сохраняется при ошибке команды.
                result = 'Ошибка: ' + (str(exc) if isinstance(exc, ValueError) else 'Проверьте параметры: /help')
            else:
                self.db.execute('INSERT OR REPLACE INTO chats VALUES (?,?)', (scope, json.dumps(state, ensure_ascii=False)))
            if event_id is not None:
                self.db.execute('INSERT INTO events VALUES (?,?)', (platform, str(event_id)))
            result = result[:3500]
            if enqueue_reply:
                # Команда, ID события и текст ответа фиксируются одной транзакцией.
                self.db.execute('INSERT INTO outbox(platform,id,chat,user,reply) VALUES (?,?,?,?,?)',
                                (platform, str(event_id), str(chat), str(user), result))
        return result
