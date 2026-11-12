"""Лимиты и приветствия проверяются управляемым временем без ожидания и сети."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from core import Service
from domain import DEFAULT_WELCOME
from guard import command_allowed
from main import bind_identity
from max_webhook import Inbox,worker_tick
from transports import Event


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'bot.db');self.now=1000
        self.service=Service(self.path,clock=lambda:self.now);self.addCleanup(self.service.db.close)

    def command(self,text,admin=False,user='42',chat='-100',platform='telegram',**kwargs):
        return self.service.execute(platform,chat,user,text,admin,**kwargs)

    def test_default_limit_warns_once_then_suppresses(self):
        for _ in range(20):self.assertIn('/rules',self.command('/help'))
        self.assertIn('Слишком много',self.command('/help'))
        for _ in range(4):self.assertIsNone(self.command('/help'))
        self.now+=60;self.assertIn('/rules',self.command('/help'))

    def test_quota_survives_restart_and_duplicates_do_not_consume_it(self):
        self.command('/setlimit 1',True)
        self.command('/help',event_id='once')
        self.service.db.close();self.service=Service(self.path,clock=lambda:self.now);self.addCleanup(self.service.db.close)
        self.assertIsNone(self.command('/help',event_id='once'))
        self.assertIn('Слишком много',self.command('/help',event_id='twice'))
        self.assertIsNone(self.command('/help',event_id='third'))

    def test_scope_user_chat_platform_and_admin_exemption(self):
        self.command('/setlimit 1',True);self.command('/help')
        self.assertIn('Слишком много',self.command('/help'))
        for kwargs in ({'user':'43'},{'chat':'-200'},{'platform':'max'},{'admin':True}):
            self.assertIn('/rules',self.command('/help',**kwargs))
        for _ in range(130):self.assertIn('/rules',self.command('/help',admin=True))

    def test_disabled_limit_and_demo_are_unrestricted(self):
        self.command('/setlimit 0',True)
        for _ in range(25):self.assertIn('/rules',self.command('/help'))
        for _ in range(25):self.assertIn('/rules',self.command('/help',platform='demo'))

    def test_invalid_and_denied_commands_count_without_mutating_rules(self):
        self.command('/setlimit 2',True)
        self.assertIn('Ошибка:',self.command('/setrules запрещено'))
        self.assertIn('Неизвестная',self.command('/bad'))
        self.assertIn('Слишком много',self.command('/agree'))
        self.assertIn('не добавил',self.command('/rules',True))

    def test_suppression_is_deduplicated_and_creates_no_extra_outbox(self):
        self.command('/setlimit 1',True)
        for i in range(4):self.command('/help',event_id=str(i),enqueue_reply=True)
        self.assertEqual(2,self.service.db.execute('SELECT count(*) FROM outbox').fetchone()[0])
        self.now+=60
        self.assertIsNone(self.command('/help',event_id='3',enqueue_reply=True))
        self.assertEqual(4,self.service.db.execute('SELECT count(*) FROM events').fetchone()[0])

    def test_maximum_tracked_users_is_bounded_and_expires(self):
        with self.service.db:
            for i in range(1000):self.assertTrue(command_allowed(self.service.db,'scope',str(i),20,self.now)[0])
            self.assertEqual((False,None),command_allowed(self.service.db,'scope','overflow',20,self.now))
            self.assertTrue(command_allowed(self.service.db,'scope','overflow',20,self.now+60)[0])
        users=json.loads(self.service.db.execute("SELECT value FROM transport_meta WHERE key='guard:scope'").fetchone()[0])
        self.assertEqual(['overflow'],list(users))

    def test_backward_clock_resets_quota_without_permanent_block(self):
        self.command('/setlimit 1',True);self.command('/help');self.command('/help')
        self.now-=120;self.assertIn('/rules',self.command('/help'))

    def test_welcome_cooldown_drops_burst_and_preserves_event_dedup(self):
        self.command('/welcomeon',True)
        self.assertEqual(DEFAULT_WELCOME,self.command('',event_id='j1',kind='join',enqueue_reply=True))
        self.now+=29
        self.assertIsNone(self.command('',event_id='j2',kind='join',enqueue_reply=True))
        self.now+=1
        self.assertIsNone(self.command('',event_id='j2',kind='join',enqueue_reply=True))
        self.assertEqual(DEFAULT_WELCOME,self.command('',event_id='j3',kind='join',enqueue_reply=True))
        self.assertEqual(2,self.service.db.execute('SELECT count(*) FROM outbox').fetchone()[0])

    def test_welcome_cooldown_survives_restart_and_can_be_disabled(self):
        self.command('/welcomeon',True);self.command('',event_id='j1',kind='join')
        self.service.db.close();self.service=Service(self.path,clock=lambda:self.now);self.addCleanup(self.service.db.close)
        self.assertIsNone(self.command('',event_id='j2',kind='join'))
        self.command('/welcomecooldown 0',True)
        self.assertEqual(DEFAULT_WELCOME,self.command('',event_id='j3',kind='join'))

    def test_cooldown_and_disabled_welcome_do_not_consume_command_quota(self):
        self.command('/setlimit 1',True)
        for i in range(5):self.assertIsNone(self.command('',event_id='disabled'+str(i),kind='join'))
        self.command('/welcomeon',True)
        self.assertEqual(DEFAULT_WELCOME,self.command('',event_id='enabled',kind='join'))
        self.assertIn('/rules',self.command('/help'))

    def test_setup_reports_missing_and_configured_values(self):
        self.assertIn('права',self.command('/setup'))
        empty=self.command('/setup',True)
        for phrase in ('не заданы','FAQ: пуст','выключено','30 с','20 за минуту'):self.assertIn(phrase,empty)
        for cmd in ('/setrules Правила','/faqadd Вопрос | Ответ','/welcomeon','/welcomecooldown 120','/setlimit 8'):
            self.command(cmd,True)
        configured=self.command('/setup',True)
        for phrase in ('версия 1','1 вопросов','включено','120 с','8 за минуту'):self.assertIn(phrase,configured)
        self.assertNotIn('версия 1',self.command('/setup',True,chat='-200'))

    def test_new_configuration_requires_admin_and_valid_bounds(self):
        for cmd in ('/setlimit 0','/welcomecooldown 0','/setup'):
            self.assertIn('права',self.command(cmd))
        for cmd in ('/setlimit 121','/setlimit -1','/setlimit 01','/setlimit 1.5','/setlimit',
                    '/welcomecooldown 86401','/welcomecooldown -1','/welcomecooldown 30 extra'):
            self.assertIn('Ошибка:',self.command(cmd,True))
        self.assertNotIn('Ошибка:',self.command('/welcomecooldown 86400',True))

    def test_max_worker_uses_quota_and_cooldown(self):
        bind_identity(self.service,'max','123');inbox=Inbox(self.path);transport=Mock()
        groups={'-100':{'admins':['42']}}
        self.command('/welcomeon',True,platform='max');self.command('/setlimit 1',True,platform='max')
        for event in (Event('-100','99','','j1','join'),Event('-100','99','','j2','join'),
                      Event('-100','99','/help','c1'),Event('-100','99','/help','c2'),Event('-100','99','/help','c3')):
            inbox.enqueue(event);worker_tick(self.service,transport,inbox,groups)
        self.assertEqual(3,transport.send.call_count)
        self.assertEqual({'done':5},inbox.status()['inbox'])


if __name__ == '__main__':unittest.main()
