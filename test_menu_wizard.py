"""Кнопки, роли и транзакционный мастер; API заменено тестовыми объектами."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch
from core import Service
from main import process_update
from menu import BUTTONS,max_keyboard,telegram_keyboard,vk_keyboard
from test_transports import max_update,vk_update
from transports import Event,Max,Telegram,TransportError,Vk


def callback(data='pushok:rules',key='click1',user=42):
    return {'update_id':10,'callback_query':{'id':key,'from':{'id':user,'is_bot':False},'data':data,
            'message':{'message_id':50,'chat':{'id':-100,'type':'supergroup'},'from':{'id':123,'is_bot':True}}}}


class MenuWizardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'bot.db');self.now=1000
        self.s=Service(self.path,clock=lambda:self.now);self.addCleanup(self.s.db.close)
        self.tg=Telegram('fake');self.tg.username='ours';self.tg.bot_id='123';self.tg.api=Mock(return_value=True)
        self.network=patch('socket.create_connection',side_effect=AssertionError('No real API'));self.network.start();self.addCleanup(self.network.stop)

    def cmd(self,text,user='42',admin=True,chat='-100',platform='telegram',**kwargs):
        return self.s.execute(platform,chat,user,text,admin,**kwargs)

    def state(self):
        row=self.s.db.execute('SELECT data FROM chats WHERE scope=?',(json.dumps(['telegram','-100']),)).fetchone()
        return json.loads(row[0]) if row else {}

    def prepare(self):
        self.cmd('/wizard')
        for value in ('Уважайте участников','Здравствуйте!','Где правила? | /rules','да','8','45'):
            self.assertNotIn('Ошибка:',self.cmd('/step '+value))

    def test_keyboard_shapes_and_readonly_navigation(self):
        tg=telegram_keyboard()['inline_keyboard'];mx=max_keyboard()[0]['payload']['buttons'];vk=json.loads(vk_keyboard())
        self.assertEqual(5,sum(map(len,tg)));self.assertEqual(5,sum(map(len,mx)))
        self.assertTrue(vk['inline']);self.assertFalse(vk['one_time'])
        for row in tg:
            for b in row:self.assertLessEqual(len(b['callback_data'].encode()),64)
        self.assertNotIn('pushok:apply',str(tg))

    def test_all_transports_attach_keyboard_to_response(self):
        for adapter in (self.tg,Max('fake'),Vk('fake',123)):
            adapter.api=Mock(return_value=True);adapter.send(Event('-100','42','','one'),'Текст')
            if adapter.platform=='telegram':self.assertIn('reply_markup',adapter.api.call_args.args[1])
            elif adapter.platform=='max':self.assertEqual(max_keyboard(),adapter.api.call_args.kwargs['body']['attachments'])
            else:self.assertEqual(json.loads(vk_keyboard()),json.loads(adapter.api.call_args.args[1]['keyboard']))

    def test_max_and_vk_labels_map_to_commands_only(self):
        for label,command in BUTTONS:
            self.assertEqual(command,Max('fake').normalize(max_update(label)).text)
            self.assertEqual(command,Vk('fake',123).normalize(vk_update(label)).text)
        for text in ('Настройка произвольная','Привет всем','/rules@other'):
            self.assertIsNone(Max('fake').normalize(max_update(text)))
            self.assertIsNone(Vk('fake',123).normalize(vk_update(text)))

    def test_telegram_callback_actor_and_identity(self):
        event=self.tg.normalize(callback('pushok:wizard',user=99))
        self.assertEqual(('99','-100','/wizard','click1'),(event.user,event.chat,event.text,event.callback_id))
        for change in (lambda x:x['callback_query'].update(data='/setrules bad'),
                       lambda x:x['callback_query']['message']['from'].update(id=456),
                       lambda x:x['callback_query']['from'].update(is_bot=True),
                       lambda x:x['callback_query']['message']['chat'].update(type='channel'),
                       lambda x:x['callback_query'].update(message=None),
                       lambda x:x['callback_query'].update(id=''),
                       lambda x:x['callback_query'].update(data={'bad':1})):
            value=callback();change(value);self.assertIsNone(self.tg.normalize(value))
        del self.tg.bot_id;self.assertIsNone(self.tg.normalize(callback()))

    def test_callback_dedup_ack_and_admin_rights(self):
        self.tg.send=Mock();groups={'-100':{'admins':['42']}}
        for _ in range(2):process_update(self.s,self.tg,groups,callback('pushok:wizard',user=99))
        self.tg.send.assert_called_once();self.assertIn('права',self.tg.send.call_args.args[1])
        self.assertNotIn('_wizard',self.state())
        self.assertEqual(2,self.tg.api.call_count)
        self.tg.api.assert_called_with('answerCallbackQuery',{'callback_query_id':'click1'})

    def test_ack_error_keeps_reply_and_unauthorized_chat_is_ignored(self):
        self.tg.send=Mock();self.tg.api.side_effect=TransportError('secret')
        process_update(self.s,self.tg,{},callback());self.tg.api.assert_not_called()
        with self.assertLogs(level='WARNING') as logs:
            process_update(self.s,self.tg,{'-100':{'admins':['42']}},callback())
        self.tg.send.assert_called_once();self.assertNotIn('secret',str(logs.output))

    def test_poll_requests_callbacks(self):
        self.tg.api.return_value=[];self.tg.poll(None)
        self.assertIn('callback_query',self.tg.api.call_args.args[1]['allowed_updates'])

    def test_wizard_applies_only_after_review(self):
        self.prepare()
        self.assertNotIn('rules',self.state());self.assertNotIn('faq',self.state())
        self.assertIn('ещё не применён',self.cmd('/wizard'))
        self.assertIn('сохранены',self.cmd('/apply',event_id='apply',enqueue_reply=True))
        s=self.state();self.assertEqual('Уважайте участников',s['rules']);self.assertEqual(1,s['version'])
        self.assertEqual(8,s['command_limit']);self.assertEqual(45,s['welcome_cooldown']);self.assertTrue(s['welcome_enabled'])
        self.assertEqual(1,len(s['faq']));self.assertNotIn('_wizard',s)
        self.assertIsNone(self.cmd('/apply',event_id='apply',enqueue_reply=True));self.assertEqual(s,self.state())

    def test_cancel_preserves_existing_configuration(self):
        self.cmd('/setrules Старые');self.cmd('/welcomeon');before=self.state()
        self.prepare();self.assertIn('отменён',self.cmd('/cancel'));self.assertEqual(before,self.state())

    def test_back_and_skip_keep_previous_value(self):
        self.cmd('/setrules Старые');self.cmd('/wizard');self.cmd('/step Новые');self.cmd('/back');self.cmd('/skip')
        for _ in range(5):self.cmd('/skip')
        self.cmd('/apply');self.assertEqual('Старые',self.state()['rules']);self.assertEqual(1,self.state()['version'])

    def test_unchanged_rules_do_not_invalidate_agreement(self):
        self.cmd('/setrules Те же');self.cmd('/agree',admin=False)
        self.cmd('/wizard');self.cmd('/step Те же')
        for _ in range(5):self.cmd('/skip')
        self.cmd('/apply');self.assertEqual(1,self.state()['version']);self.assertIn('приняты',self.cmd('/mine'))

    def test_owner_permissions_chat_isolation_and_expiry(self):
        self.cmd('/wizard')
        self.assertIn('другой администратор',self.cmd('/wizard',user='43'))
        self.assertIn('другому администратору',self.cmd('/cancel',user='43'))
        self.assertIn('права',self.cmd('/step Текст',admin=False))
        self.assertIn('Нет действующего',self.cmd('/step Текст',chat='-200'))
        self.assertIn('Нет действующего',self.cmd('/step Текст',platform='max'))
        self.now+=1800;self.assertIn('Нет действующего',self.cmd('/apply'))
        self.assertIn('1/6',self.cmd('/wizard',user='43'))

    def test_manual_change_conflicts_but_user_agreement_does_not(self):
        self.prepare();self.cmd('/setwelcome Другой текст',user='43')
        self.assertIn('изменились',self.cmd('/apply'));self.assertNotIn('rules',self.state())
        self.cmd('/cancel');self.cmd('/setrules Правила');self.prepare();self.cmd('/agree',admin=False,user='99')
        self.assertIn('сохранены',self.cmd('/apply'))

    def test_resume_after_restart_and_view_complete_draft(self):
        self.cmd('/wizard');self.cmd('/step '+'я'*1000)
        self.s.db.close();self.s=Service(self.path,clock=lambda:self.now);self.addCleanup(self.s.db.close)
        self.assertIn('2/6',self.cmd('/wizard'));self.assertEqual('я'*1000,self.cmd('/draft rules'))

    def test_invalid_input_does_not_advance_step(self):
        self.cmd('/wizard');before=self.state()
        for cmd in ('/step','/step '+'x'*2801,'/apply','/skip extra','/back extra'):
            self.assertIn('Ошибка:',self.cmd(cmd));self.assertEqual(before,self.state())
        self.cmd('/skip');self.cmd('/skip')
        self.assertIn('Ошибка:',self.cmd('/step Без разделителя'))
        self.cmd('/skip');self.assertIn('Ошибка:',self.cmd('/step maybe'))
        self.cmd('/skip');self.assertIn('Ошибка:',self.cmd('/step 121'))

    def test_apply_and_outbox_are_atomic(self):
        self.prepare();before=self.state()
        self.s.db.execute("CREATE TRIGGER fail_reply BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.cmd('/apply',event_id='atomic',enqueue_reply=True)
        self.assertEqual(before,self.state());self.assertEqual(0,self.s.db.execute('SELECT count(*) FROM events').fetchone()[0])

    def test_legacy_queued_event_can_be_loaded(self):
        self.assertIsNone(Event(**{'chat':'-100','user':'42','text':'/help','id':'old'}).callback_id)


if __name__=='__main__':unittest.main()
