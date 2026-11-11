"""Webhook/SQLite проверки без доступа к реальным чатам."""
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from core import Service
from main import bind_identity, metadata
from max_webhook import Inbox, WebhookApp, MAX_BODY, register, recover_interrupted_sends, secret_from_env, worker_tick
from transports import Event, TransportError
from test_transports import max_update

SECRET = 'test-only-' + 'x' * 40


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name)/'bot.db')
        self.service = Service(self.path)
        self.addCleanup(self.service.db.close)
        bind_identity(self.service,'max','123')
        self.inbox = Inbox(self.path,limit=5)
        self.groups = {'-100':{'admins':['42']}}
        self.app = WebhookApp(self.inbox,self.groups,SECRET)
        self.transport = Mock()
        self.network = patch('socket.create_connection',side_effect=AssertionError('External network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def request(self, update=None, **overrides):
        body = json.dumps(max_update() if update is None else update).encode()
        env = {'PATH_INFO':'/webhooks/max','REQUEST_METHOD':'POST','HTTP_X_MAX_BOT_API_SECRET':SECRET,
               'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body)}
        env.update(overrides)
        response = []
        result = b''.join(self.app(env,lambda status,headers:response.append(status)))
        return response[0],json.loads(result)

    def count(self, table):
        return self.service.db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def tick(self):
        return worker_tick(self.service,self.transport,self.inbox,self.groups)

    def test_accepts_only_after_durable_enqueue(self):
        self.assertEqual('200 OK',self.request()[0])
        self.assertEqual(1,self.count('webhook_inbox'))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM webhook_inbox').fetchone()[0])
        self.assertEqual(0,self.count('chats'))
        self.transport.send.assert_not_called()

    def test_wrong_or_missing_secret_never_reads_body(self):
        body = Mock()
        for value in ('','incorrect','ю'*32):
            self.assertEqual('403 Forbidden',self.request(HTTP_X_MAX_BOT_API_SECRET=value,**{'wsgi.input':body})[0])
        body.read.assert_not_called()
        self.assertEqual(0,self.count('webhook_inbox'))

    def test_http_validation(self):
        for changes,expected in [({'PATH_INFO':'/other'},'404'),({'REQUEST_METHOD':'GET'},'405'),
                                 ({'CONTENT_TYPE':'text/plain'},'415'),({'CONTENT_LENGTH':''},'411'),
                                 ({'CONTENT_LENGTH':'-1'},'411'),({'CONTENT_LENGTH':str(MAX_BODY+1)},'413'),
                                 ({'CONTENT_LENGTH':'1','wsgi.input':io.BytesIO(b'{')},'400'),
                                 ({'CONTENT_LENGTH':'8','wsgi.input':io.BytesIO(b'{}')},'400')]:
            self.assertTrue(self.request(**changes)[0].startswith(expected),changes)
        self.assertEqual(0,self.count('webhook_inbox'))

    def test_invalid_root_json_is_rejected(self):
        for value in ([],True,{'bad':'shape'}):
            self.assertEqual('400 Bad Request',self.request(value)[0])

    def test_other_chats_and_noncommands_are_ignored(self):
        for update in (max_update(chat=-200),max_update(text='привет'),{'update_type':'bot_added'}):
            self.assertEqual('200 OK',self.request(update)[0])
        self.assertEqual(0,self.count('webhook_inbox'))

    def test_commands_addressed_to_this_bot_only(self):
        self.app = WebhookApp(self.inbox,self.groups,SECRET,username='pushok_bot')
        self.request(max_update(text='/help@other_bot',mid='foreign'))
        self.assertEqual(0,self.count('webhook_inbox'))
        self.request(max_update(text='/help@pushok_bot',mid='own'))
        self.tick()
        self.assertEqual(1,self.count('events'))
        self.transport.send.assert_called_once()

    def test_duplicate_delivery_before_and_after_processing(self):
        self.request()
        self.request()
        self.tick()
        self.request()
        self.tick()
        self.assertEqual(1,self.count('webhook_inbox'))
        self.assertEqual(1,self.count('events'))
        self.transport.send.assert_called_once()

    def test_full_queue_retries_but_duplicate_is_accepted(self):
        self.inbox.limit = 1
        self.assertEqual('200 OK',self.request()[0])
        self.assertEqual('200 OK',self.request()[0])
        self.assertEqual('503 Service Unavailable',self.request(max_update(mid='second'))[0])
        self.tick()
        self.assertEqual('200 OK',self.request(max_update(mid='second'))[0])

    def test_disk_failure_never_acknowledges(self):
        with patch.object(self.inbox,'enqueue',side_effect=sqlite3.OperationalError('private')):
            with self.assertLogs(level='ERROR') as logs:
                status,body = self.request()
        self.assertEqual('503 Service Unavailable',status)
        self.assertNotIn('private',str(body)+str(logs.output))

    def test_profile_scenario_over_webhook(self):
        scenarios = json.loads(Path(__file__).with_name('scenario.json').read_text())
        for index,(text,admin,expected) in enumerate(scenarios):
            self.groups['-100']['admins'] = ['42'] if admin else ['43']
            self.request(max_update(text=text.replace('alice','42'),mid=f'case.{index}'))
            self.tick()
            self.assertIn(expected,self.transport.send.call_args.args[1])
        self.assertEqual(len(scenarios),self.transport.send.call_count)

    def test_command_and_outbox_commit_together(self):
        command,admin,_ = json.loads(Path(__file__).with_name('scenario.json').read_text())[0]
        self.service.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.execute('max','-100','42',command,admin,'atomic',enqueue_reply=True)
        self.assertEqual(0,self.count('chats'))
        self.assertEqual(0,self.count('events'))
        self.assertEqual(0,self.count('outbox'))

    def test_restart_recovers_pending_reply_without_reapplying_command(self):
        self.request()
        event = Event(**json.loads(self.service.db.execute('SELECT event FROM webhook_inbox').fetchone()[0]))
        self.service.execute('max',event.chat,event.user,event.text,False,event.id,enqueue_reply=True)
        before = self.service.db.execute('SELECT * FROM chats').fetchall()
        # Имитируем аварию между COMMIT команды и пометкой входящего события done.
        self.tick()
        self.assertEqual(before,self.service.db.execute('SELECT * FROM chats').fetchall())
        self.assertEqual(1,self.count('events'))
        self.transport.send.assert_called_once()

    def test_unconfirmed_send_is_not_retried(self):
        self.transport.send.side_effect = TransportError('unknown')
        self.request()
        with self.assertLogs(level='ERROR'):
            self.tick()
        self.tick()
        self.assertEqual({'uncertain':1},self.inbox.status()['outbox'])
        self.transport.send.assert_called_once()

    def test_crash_during_send_becomes_uncertain(self):
        self.service.execute('max','-100','42','/help',False,'crash',enqueue_reply=True)
        with self.service.db:
            self.service.db.execute("UPDATE outbox SET status='sending'")
        recover_interrupted_sends(self.service)
        self.tick()
        self.assertEqual({'uncertain':1},self.inbox.status()['outbox'])
        self.transport.send.assert_not_called()

    def test_removed_chat_cancels_pending_response(self):
        self.service.execute('max','-100','42','/help',False,'removed',enqueue_reply=True)
        self.groups.clear()
        self.tick()
        self.assertEqual({'cancelled':1},self.inbox.status()['outbox'])
        self.transport.send.assert_not_called()

    def test_bad_event_does_not_block_later_events_forever(self):
        self.request()
        with patch.object(self.service,'execute',side_effect=ValueError('private')):
            with self.assertLogs(level='ERROR'):
                for _ in range(3): self.tick()
        self.assertEqual({'failed':1},self.inbox.status()['inbox'])
        self.request(max_update(mid='second'))
        self.tick()
        self.transport.send.assert_called_once()

    def test_long_command_is_also_deduplicated(self):
        self.request(max_update(text='/'+('x'*3001)))
        self.tick()
        self.request(max_update(text='/'+('x'*3001)))
        self.tick()
        self.transport.send.assert_called_once()
        self.assertIn('3000',self.transport.send.call_args.args[1])

    def test_health_requires_recent_worker_heartbeat(self):
        self.assertEqual('503 Service Unavailable',self.request(PATH_INFO='/healthz',REQUEST_METHOD='GET')[0])
        self.tick()
        self.assertEqual('200 OK',self.request(PATH_INFO='/healthz',REQUEST_METHOD='GET')[0])
        metadata(self.service,'max:webhook_heartbeat',time.time()-100,write=True)
        self.assertEqual('503 Service Unavailable',self.request(PATH_INFO='/healthz',REQUEST_METHOD='GET')[0])

    def test_secret_and_registration_validation(self):
        for secret in ('','short','x'*257,'x'*40+'!'):
            with self.assertRaises(ValueError): secret_from_env({'MAX_WEBHOOK_SECRET':secret})
        for url in ('http://example.org/webhooks/max','https://example.org:443/webhooks/max',
                    'https://user:pass@example.org/webhooks/max','https://example.org/other',
                    'https://example.org/webhooks/max?secret=x'):
            with self.assertRaises(ValueError): register(self.transport,url,SECRET)
        self.transport.api.assert_not_called()

    def test_existing_foreign_subscription_is_preserved(self):
        self.transport.api.return_value = {'subscriptions':[{'url':'https://other.org/hook'}]}
        with self.assertRaises(ValueError): register(self.transport,'https://example.org/webhooks/max',SECRET)
        self.transport.api.assert_called_once_with('subscriptions')

    def test_register_payload_and_confirmation(self):
        self.transport.api.side_effect = [{'subscriptions':[]},{'success':True}]
        register(self.transport,'https://example.org/webhooks/max',SECRET)
        self.assertEqual({'url':'https://example.org/webhooks/max','secret':SECRET,'update_types':['message_created']},
                         self.transport.api.call_args.kwargs['body'])


if __name__ == '__main__': unittest.main()
