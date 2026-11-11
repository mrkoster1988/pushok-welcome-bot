"""Контрактные проверки на синтетических событиях; реальные токены не нужны."""
import copy
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch
from core import Service
from main import bind_identity, metadata, poll_once, process_update
from settings import credentials, load_groups
from transports import Event, Max, NoRedirect, Telegram, TransportError, Vk, http_json


def max_update(text='/help', chat=-100, user=42, mid='mid.1'):
    return {'update_type': 'message_created', 'message': {
        'sender': {'user_id': user, 'is_bot': False},
        'recipient': {'chat_id': chat, 'chat_type': 'chat'},
        'body': {'mid': mid, 'text': text}}}


def vk_update(text='/help', chat=2000000001, user=42, cmid=1):
    return {'type': 'message_new', 'group_id': 123, 'object': {'message': {
        'peer_id': chat, 'from_id': user, 'conversation_message_id': cmid, 'text': text, 'out': 0}}}


class TransportTests(unittest.TestCase):
    def setUp(self):
        # Любой случайный сетевой запрос в тесте должен немедленно завершиться ошибкой.
        self.network = patch('socket.create_connection', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.service = Service(':memory:')
        self.addCleanup(self.service.db.close)

    def test_max_normalization(self):
        event = Max('fake').normalize(max_update())
        self.assertEqual(Event('-100', '42', '/help', 'mid.1'), event)

    def test_max_rejects_channels_bots_missing_authors_and_noncommands(self):
        base = max_update()
        mutations = [lambda e: e.update(update_type='message_edited'),
                     lambda e: e['message']['recipient'].update(chat_type='channel'),
                     lambda e: e['message']['recipient'].update(chat_type='dialog'),
                     lambda e: e['message']['sender'].update(is_bot=True),
                     lambda e: e['message'].update(sender=None),
                     lambda e: e['message'].update(body=None),
                     lambda e: e['message']['body'].update(text=None),
                     lambda e: e['message']['body'].update(text='обычный текст'),
                     lambda e: e['message']['body'].pop('mid')]
        for mutate in mutations:
            event = copy.deepcopy(base)
            mutate(event)
            self.assertIsNone(Max('fake').normalize(event))

    def test_vk_event_identity_is_scoped_by_chat(self):
        transport = Vk('fake', 123)
        one = transport.normalize(vk_update())
        two = transport.normalize(vk_update(chat=2000000002))
        self.assertNotEqual(one.id, two.id)
        self.assertEqual(('2000000001', '42', '/help'), (one.chat, one.user, one.text))

    def test_vk_rejects_foreign_groups_outgoing_and_anonymous_senders(self):
        for change in ({'out': 1}, {'from_id': -123}, {'from_id': True},
                       {'from_id': None}, {'action': {'type': 'chat_invite_user'}},
                       {'text': None}, {'text': '/help@another'}, {'conversation_message_id': None}):
            event = vk_update()
            event['object']['message'].update(change)
            self.assertIsNone(Vk('fake', 123).normalize(event))
        event = vk_update()
        event['group_id'] = 999
        self.assertIsNone(Vk('fake', 123).normalize(event))

    def test_malformed_events_do_not_crash(self):
        telegram = Telegram('fake')
        telegram.username = 'ours'
        for transport in (telegram, Max('fake'), Vk('fake', 123)):
            for event in (None, [], 42, {}, {'message': None}, {'object': None}):
                self.assertIsNone(transport.normalize(event))

    def test_platform_scenarios_use_real_normalization_and_rights(self):
        scenarios = json.loads(Path(__file__).with_name('scenario.json').read_text())
        for transport, make, chat in ((Max('fake'), max_update, '-100'), (Vk('fake', 123), vk_update, '2000000001')):
            transport.send = Mock()
            groups = {chat: {'admins': ['42']}}
            for i, (command, admin, expected) in enumerate(scenarios, 1):
                command = command.replace('alice', '42')
                event = make(command)
                if transport.platform == 'max':
                    event['message']['body']['mid'] = f'mid.{i}'
                else:
                    event['object']['message']['conversation_message_id'] = i
                # Роль приходит из allowlist, а не из поля события.
                groups[chat]['admins'] = ['42'] if admin else ['43']
                process_update(self.service, transport, groups, event)
                self.assertIn(expected, transport.send.call_args.args[1])

    def test_unauthorized_chat_has_no_side_effect(self):
        transport = Max('fake')
        transport.send = Mock()
        process_update(self.service, transport, {'-2': {'admins': ['42']}}, max_update())
        transport.send.assert_not_called()
        self.assertEqual([], self.service.db.execute('SELECT * FROM chats').fetchall())

    def test_unlisted_admin_cannot_mutate(self):
        commands = [r[0] for r in json.loads(Path(__file__).with_name('scenario.json').read_text()) if r[1]]
        for platform, make, chat in ((Max('fake'), max_update, '-100'), (Vk('fake',123), vk_update, '2000000001')):
            platform.send = Mock()
            for index, command in enumerate(commands, 1):
                event = make(command, user=99)
                if platform.platform == 'max':
                    event['message']['body']['mid'] = f'admin.{index}'
                else:
                    event['object']['message']['conversation_message_id'] = index
                process_update(self.service, platform, {chat: {'admins': ['42']}}, event)
                self.assertIn('Ошибка:', platform.send.call_args.args[1])

    def test_duplicate_event_is_not_sent_again(self):
        transport = Max('fake')
        transport.send = Mock()
        for _ in range(2):
            process_update(self.service, transport, {'-100': {'admins': ['42']}}, max_update())
        transport.send.assert_called_once()

    def test_unknown_send_outcome_is_not_retried_or_rolled_back(self):
        transport = Max('fake')
        transport.send = Mock(side_effect=TransportError('network'))
        bind_identity(self.service, 'max', '123')
        transport.poll = Mock(return_value=([max_update()], 100))
        with self.assertLogs(level='ERROR'):
            self.assertEqual(100, poll_once(self.service, transport, {'-100': {'admins': ['42']}}, None))
        self.assertEqual(100, metadata(self.service, 'max:cursor'))
        process_update(self.service, transport, {'-100': {'admins': ['42']}}, max_update())
        transport.send.assert_called_once()

    def test_processing_failure_does_not_advance_cursor(self):
        bind_identity(self.service, 'max', '123')
        transport = Max('fake')
        transport.poll = Mock(return_value=([max_update()], 200))
        with patch.object(self.service, 'execute', side_effect=RuntimeError('db')):
            with self.assertRaises(RuntimeError):
                poll_once(self.service, transport, {'-100': {'admins': ['42']}}, None)
        self.assertIsNone(metadata(self.service, 'max:cursor'))

    def test_cursor_and_binding_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'test.db')
            service = Service(path)
            bind_identity(service, 'max', '123')
            metadata(service, 'max:cursor', 400, write=True)
            service.db.close()
            service = Service(path)
            try:
                bind_identity(service, 'max', '123')
                self.assertEqual(400, metadata(service, 'max:cursor'))
                with self.assertRaises(ValueError):
                    bind_identity(service, 'max', '456')
            finally:
                service.db.close()

    @patch('transports.http_json')
    def test_max_authentication_and_send_contract(self, request):
        request.return_value = {'message': {}}
        Max('SENTINEL').send(Event('-100','42','/help','mid'), 'Привет')
        args, kwargs = request.call_args
        self.assertEqual('https://platform-api2.max.ru/messages', args[0])
        self.assertEqual({'Authorization': 'SENTINEL'}, kwargs['headers'])
        self.assertEqual({'chat_id': '-100'}, kwargs['query'])
        self.assertEqual({'text': 'Привет'}, kwargs['body'])
        self.assertNotIn('SENTINEL', args[0])

    def test_max_webhook_blocks_polling_without_changing_subscription(self):
        transport = Max('fake')
        transport.api = Mock(side_effect=[{'user_id': 123}, {'subscriptions': [{'url': 'https://example.org'}]}])
        with self.assertRaises(TransportError):
            transport.start()
        self.assertEqual(['me', 'subscriptions'], [c.args[0] for c in transport.api.call_args_list])

    def test_max_marker_and_event_type_filter(self):
        transport = Max('fake')
        transport.api = Mock(return_value={'updates': [], 'marker': 201})
        self.assertEqual(([],201), transport.poll(200))
        self.assertEqual(200, transport.api.call_args.kwargs['query']['marker'])
        self.assertEqual('message_created', transport.api.call_args.kwargs['query']['types'])

    @patch('transports.http_json')
    def test_vk_send_uses_form_token_and_stable_random_id(self, request):
        request.return_value = {'response': 1}
        transport = Vk('SENTINEL',123)
        event = transport.normalize(vk_update())
        transport.send(event,'Привет')
        first = request.call_args.kwargs['form']
        transport.send(event,'Привет')
        self.assertEqual(first['random_id'],request.call_args.kwargs['form']['random_id'])
        self.assertEqual('2000000001',first['peer_id'])
        self.assertEqual('5.199', first['v'])
        self.assertEqual('SENTINEL',first['access_token'])
        self.assertNotIn('SENTINEL',request.call_args.args[0])

    @patch('transports.http_json')
    def test_vk_recovers_poll_failures(self, request):
        for failed, expected in ((1,'90'),(2,'80'),(3,'100')):
            transport = Vk('fake',123)
            transport.server = {'server':'https://lp.vk.com/wh/123','key':'old','ts':'70'}
            transport.api = Mock(return_value={'server':'https://lp.vk.com/wh/123','key':'new','ts':'100'})
            request.return_value = {'failed': failed,'ts':'90'}
            self.assertEqual(([],expected),transport.poll('80'))
            if failed == 1:
                transport.api.assert_not_called()
            else:
                self.assertEqual('new',transport.server['key'])

    def test_vk_does_not_send_keys_to_untrusted_servers(self):
        for url in ('http://lp.vk.com/a','https://vk.com.evil.test/a','https://localhost/a',
                    'https://user@lp.vk.com/a','https://lp.vk.com/a?token=x'):
            transport = Vk('fake',123)
            transport.api = Mock(return_value={'server':url,'key':'secret','ts':'1'})
            with self.assertRaises(TransportError):
                transport.start()

    def test_telegram_webhook_still_blocks_polling(self):
        transport = Telegram('fake')
        transport.api = Mock(side_effect=[{'id':123,'username':'ours'},{'url':'https://example.org'}])
        with self.assertRaises(TransportError):
            transport.start()

    @patch('transports.urllib.request.build_opener')
    def test_http_form_serialization_and_error_redaction(self, opener):
        response = io.BytesIO(b'{"response":1}')
        opener.return_value.open.return_value = response
        self.assertEqual({'response':1},http_json('https://api.vk.com/method/messages.send',method='POST',form={'access_token':'SENTINEL','message':'Привет'}))
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual('Привет', urllib.parse.parse_qs(request.data.decode())['message'][0])
        opener.return_value.open.side_effect = RuntimeError('URL contains SENTINEL')
        with self.assertRaises(TransportError) as caught:
            http_json('https://example.org/SENTINEL')
        self.assertNotIn('SENTINEL',str(caught.exception))

    def test_redirects_are_rejected(self):
        with self.assertRaises(TransportError):
            NoRedirect().redirect_request(None,None,302,'',{},'https://other.example')


class SettingsTests(unittest.TestCase):
    def load(self, value, platform):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'groups.json'
            file.write_text(json.dumps(value))
            return load_groups(file,platform)

    def test_platform_admins_do_not_cross(self):
        config = {'telegram':{'123':{'admins':[1]}},'max':{'123':{'admins':[2]}}}
        self.assertEqual(['1'],self.load(config,'telegram')['123']['admins'])
        self.assertEqual(['2'],self.load(config,'max')['123']['admins'])

    def test_legacy_format_is_telegram_only(self):
        config = {'-123':{'admins':[1]}}
        self.assertIn('-123',self.load(config,'telegram'))
        for platform in ('max','vk'):
            with self.assertRaises(ValueError): self.load(config,platform)

    def test_invalid_ids_and_empty_admins_fail_closed(self):
        for value in ({},[],{'REPLACE_CHAT_ID':{'admins':[1]}},{'-1':{'admins':[]}},
                      {'-1':{'admins':[True]}},{'-1':{'admins':['01']}},{'-1':{'admins':[-2]}}):
            with self.assertRaises(ValueError): self.load(value,'telegram')

    def test_credentials_are_checked_without_network(self):
        self.assertEqual('fake',credentials('vk',{'VK_TOKEN':'fake','VK_GROUP_ID':'123'}))
        for env in ({},{'VK_TOKEN':'fake'},{'VK_TOKEN':'fake','VK_GROUP_ID':'club123'}):
            with self.assertRaises(ValueError): credentials('vk',env)


if __name__ == '__main__':
    unittest.main()
