"""Текстовые команды Telegram, MAX (тестовый polling) и VK Bots Long Poll."""
import hashlib
import json
import logging
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class TransportError(RuntimeError):
    """Сообщение без токенов, URL запросов и ответов провайдера."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TransportError('API вернул перенаправление; запрос остановлен.')


def http_json(url, *, method='GET', query=None, body=None, form=None, headers=None, ca_file=None):
    if query:
        url += '?' + urllib.parse.urlencode(query)
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        request_headers['Content-Type'] = 'application/json'
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        context = ssl.create_default_context()
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        with opener.open(request, timeout=40) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise TransportError('Неожиданный формат ответа API.')
        return result
    except TransportError:
        raise
    except Exception:
        raise TransportError('Запрос API не выполнен: проверьте сеть, токен и доверенные TLS-сертификаты.') from None


def obj(value):
    return value if isinstance(value, dict) else {}


def numeric_id(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if len(text) > 20 or not text.lstrip('-').isascii() or not text.lstrip('-').isdigit():
        return None
    number = int(text)
    if str(number) != text or number == 0 or abs(number) >= 2**63 or (positive and number < 1):
        return None
    return text


def command_text(text, username=None):
    if not isinstance(text, str) or not text.startswith('/'):
        return None
    pieces = text.split(maxsplit=1)
    command = pieces[0]
    if '@' in command:
        command, target = command.split('@', 1)
        if not username or target.casefold() != username.casefold():
            return None
    return command + (' ' + pieces[1] if len(pieces) == 2 else '')


@dataclass(frozen=True)
class Event:
    chat: str
    user: str
    text: str
    id: str


def normalize(update, username):
    """Совместимость с тестами alpha.1; только обычные сообщения пользователя."""
    message = obj(obj(update).get('message'))
    author = obj(message.get('from'))
    if author.get('is_bot') or message.get('sender_chat'):
        return None
    chat = numeric_id(obj(message.get('chat')).get('id'))
    user = numeric_id(author.get('id'), positive=True)
    text = command_text(message.get('text'), username)
    return (chat, user, text) if chat and user and text else None


class Telegram:
    platform = 'telegram'

    def __init__(self, token):
        self.token = token

    def api(self, method, payload):
        data = http_json('https://api.telegram.org/bot' + self.token + '/' + method,
                         method='POST', body=payload)
        if data.get('ok') is not True or 'result' not in data:
            raise TransportError('Telegram API отклонил запрос.')
        return data['result']

    def start(self):
        me = self.api('getMe', {})
        hook = self.api('getWebhookInfo', {})
        if hook.get('url'):
            raise TransportError('Telegram: уже настроен webhook. Нужен отдельный бот без webhook.')
        self.username = me['username']
        return str(me['id'])

    def poll(self, cursor):
        updates = self.api('getUpdates', {'offset': cursor or 0, 'timeout': 25, 'allowed_updates': ['message']})
        if not isinstance(updates, list) or any(type(obj(x).get('update_id')) is not int for x in updates):
            raise TransportError('Telegram: неверный формат событий.')
        return updates, max(x['update_id'] for x in updates) + 1 if updates else cursor

    def normalize(self, update):
        result = normalize(update, self.username)
        return Event(*result, str(update['update_id'])) if result else None

    def send(self, event, text):
        self.api('sendMessage', {'chat_id': event.chat, 'text': text})


class Max:
    platform = 'max'

    def __init__(self, token, ca_file=None):
        self.token, self.ca_file = token, ca_file

    def api(self, path, *, method='GET', query=None, body=None):
        result = http_json('https://platform-api2.max.ru/' + path, method=method, query=query,
                           body=body, headers={'Authorization': self.token}, ca_file=self.ca_file)
        if 'code' in result or result.get('success') is False:
            raise TransportError('MAX API отклонил запрос.')
        return result

    def start(self):
        me = self.api('me')
        subscriptions = self.api('subscriptions')
        if not isinstance(subscriptions.get('subscriptions'), list):
            raise TransportError('MAX: не удалось проверить webhook-подписки.')
        if subscriptions['subscriptions']:
            raise TransportError('MAX: уже настроен webhook. Polling остановлен; подписки не изменены.')
        logging.warning('MAX polling предназначен только для тестирования. Для постоянной работы нужен webhook.')
        self.username = me.get('username')
        return str(me['user_id'])

    def poll(self, cursor):
        query = {'timeout': 25, 'limit': 100, 'types': 'message_created'}
        if cursor is not None:
            query['marker'] = cursor
        data = self.api('updates', query=query)
        if not isinstance(data.get('updates'), list) or 'marker' not in data:
            raise TransportError('MAX: неверный формат событий.')
        return data['updates'], data['marker'] if data['marker'] is not None else cursor

    def normalize(self, update):
        if obj(update).get('update_type') != 'message_created':
            return None
        message = obj(update.get('message'))
        sender, recipient, body = (obj(message.get(k)) for k in ('sender', 'recipient', 'body'))
        # Каналы, комментарии и личные диалоги будут отдельными сценариями.
        if recipient.get('chat_type') != 'chat' or sender.get('is_bot'):
            return None
        chat = numeric_id(recipient.get('chat_id'))
        user = numeric_id(sender.get('user_id'), positive=True)
        text = command_text(body.get('text'), getattr(self, 'username', None))
        mid = body.get('mid')
        if chat and user and text and isinstance(mid, str) and mid:
            return Event(chat, user, text, mid)
        return None

    def send(self, event, text):
        self.api('messages', method='POST', query={'chat_id': event.chat}, body={'text': text})


class Vk:
    platform = 'vk'

    def __init__(self, token, group_id):
        self.token, self.group_id = token, str(group_id)
        self.server = None

    def api(self, method, payload):
        result = http_json('https://api.vk.com/method/' + method, method='POST',
                           form={**payload, 'access_token': self.token, 'v': '5.199'})
        if 'error' in result or 'response' not in result:
            raise TransportError('VK API отклонил запрос. Проверьте ключ сообщества и права.')
        return result['response']

    def refresh(self):
        data = self.api('groups.getLongPollServer', {'group_id': self.group_id})
        url = urllib.parse.urlsplit(data.get('server', ''))
        if (url.scheme != 'https' or not url.hostname or
                not (url.hostname.endswith('.vk.com') or url.hostname == 'vk.com') or
                url.username or url.password or url.port not in (None, 443) or url.query or url.fragment or
                not data.get('key') or 'ts' not in data):
            raise TransportError('VK: неожиданный адрес или параметры Long Poll сервера.')
        self.server = data

    def start(self):
        self.refresh()
        return self.group_id

    def poll(self, cursor):
        if self.server is None:
            self.refresh()
        ts = cursor if cursor is not None else self.server['ts']
        data = http_json(self.server['server'], query={'act': 'a_check', 'key': self.server['key'],
                                                      'ts': ts, 'wait': 25})
        failed = data.get('failed')
        if failed == 1 and 'ts' in data:
            logging.warning('VK: курсор устарел; часть событий могла быть пропущена.')
            return [], data['ts']
        if failed in (2, 3):
            self.refresh()
            if failed == 3:
                logging.warning('VK: история Long Poll недоступна; часть событий могла быть пропущена.')
            return [], self.server['ts'] if failed == 3 else cursor
        if failed or not isinstance(data.get('updates'), list) or 'ts' not in data:
            raise TransportError('VK: неверный ответ Long Poll сервера.')
        return data['updates'], data['ts']

    def normalize(self, update):
        if (obj(update).get('type') != 'message_new' or
                str(update.get('group_id')) != self.group_id):
            return None
        message = obj(obj(update.get('object')).get('message'))
        chat = numeric_id(message.get('peer_id'), positive=True)
        user = numeric_id(message.get('from_id'), positive=True)
        text = command_text(message.get('text'))
        if not chat or not user or not text or message.get('out') or message.get('action'):
            return None
        cmid = numeric_id(message.get('conversation_message_id'), positive=True)
        mid = numeric_id(message.get('id'), positive=True)
        # ID обязательно привязан к беседе: одинаковый cmid бывает в разных чатах.
        event_id = f'{self.group_id}:{chat}:cmid:{cmid}' if cmid else (
            f'{self.group_id}:{chat}:id:{mid}' if mid else None)
        return Event(chat, user, text, event_id) if event_id else None

    def send(self, event, text):
        random_id = int.from_bytes(hashlib.sha256(event.id.encode()).digest()[:4], 'big') & 0x7fffffff
        self.api('messages.send', {'peer_id': event.chat, 'message': text, 'random_id': random_id or 1})
