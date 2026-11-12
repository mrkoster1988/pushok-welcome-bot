"""Профильные сценарии FAQ и вступления на синтетических событиях платформ."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from core import Service
from domain import DEFAULT_WELCOME
from main import bind_identity,process_update
from max_webhook import Inbox,WebhookApp,worker_tick
from transports import Event,Max,Telegram,Vk


def tg_join(uid=42,update_id=101):
    return {'update_id':update_id,'message':{'chat':{'id':-100,'type':'supergroup'},
            'from':{'id':99,'is_bot':False},'new_chat_members':[{'id':uid,'is_bot':False}]}}


def max_join(uid=42,stamp=1794474000000):
    return {'update_type':'user_added','chat_id':-100,'timestamp':stamp,'is_channel':False,
            'user':{'user_id':uid,'is_bot':False,'first_name':'Участник'},'inviter_id':99}


def vk_join(uid=42,cmid=101):
    return {'type':'message_new','group_id':123,'object':{'message':{'peer_id':2000000001,
            'from_id':99,'conversation_message_id':cmid,'out':0,'text':'',
            'action':{'type':'chat_invite_user','member_id':uid}}}}


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=str(Path(self.temp.name)/'bot.db')
        self.service=Service(self.path);self.addCleanup(self.service.db.close)
        bind_identity(self.service,'max','123')
        self.inbox=Inbox(self.path)
        self.network=patch('socket.create_connection',side_effect=AssertionError('No live API'))
        self.network.start();self.addCleanup(self.network.stop)

    def command(self,text,admin=False,platform='max',chat='-100',user='42',**kwargs):
        return self.service.execute(platform,chat,user,text,admin,**kwargs)

    def state(self,platform='max',chat='-100'):
        row=self.service.db.execute('SELECT data FROM chats WHERE scope=?',(json.dumps([platform,chat]),)).fetchone()
        return json.loads(row[0]) if row else {}

    def adapters(self):
        tg=Telegram('test');tg.username='ours'
        return [(tg,tg_join,'-100'),(Max('test'),max_join,'-100'),(Vk('test',123),vk_join,'2000000001')]

    def test_faq_complete_management(self):
        self.assertIn('пуст',self.command('/faq'))
        self.assertIn('#1',self.command('/faqadd Где правила? | Введите /rules',True))
        self.assertIn('Где правила?',self.command('/faq'))
        self.assertIn('Введите /rules',self.command('/answer 1'))
        self.assertIn('обновлён',self.command('/faqedit 1 | Новые правила? | Ответ\nВторая строка | продолжение',True))
        self.assertIn('Вторая строка | продолжение',self.command('/answer 1'))
        self.assertIn('удалён',self.command('/faqdel 1',True))
        self.assertIn('не найден',self.command('/answer 1'))
        self.assertIn('#2',self.command('/faqadd Новый вопрос | Новый ответ',True))

    def test_read_commands_available_to_regular_member(self):
        self.command('/faqadd Где помощь? | У администратора',True)
        for command in ('/faq','/answer 1','/faqfind помощь','/welcome'):
            self.assertFalse(self.command(command,user='99').startswith('Ошибка:'))

    def test_all_mutations_require_admin_and_do_not_change_state(self):
        self.command('/faqadd Вопрос | Ответ',True)
        before=self.state()
        for command in ('/faqadd Другой | Ответ','/faqedit 1 | Новый | Ответ','/faqdel 1',
                        '/setwelcome Привет','/welcomeon','/welcomeoff','/setrules Правила'):
            self.assertIn('права администратора',self.command(command,user='99'))
            self.assertEqual(before,self.state())

    def test_question_answer_limits_and_failed_edit_rollback(self):
        self.command('/faqadd Вопрос | Ответ',True)
        before=self.state()
        for arg in ('нет разделителя',' | Ответ','Вопрос | ','В\nопрос | Ответ','x'*181+' | Ответ','Вопрос | '+'x'*1601):
            self.assertIn('Ошибка:',self.command('/faqadd '+arg,True))
            self.assertEqual(before,self.state())
        self.assertIn('Ошибка:',self.command('/faqedit 1 | Новый | ',True))
        self.assertEqual(before,self.state())

    def test_faq_page_boundaries_and_invalid_identifiers(self):
        for i in range(6):self.command(f'/faqadd Вопрос {i} | Ответ {i}',True)
        first=self.command('/faq');second=self.command('/faq 2')
        self.assertIn('страница 1/2',first);self.assertNotIn('#6.',first)
        self.assertIn('#6.',second);self.assertNotIn('#5.',second)
        for command in ('/faq 0','/faq 3','/faq -1','/answer x','/answer 01','/faqdel 9','/faqedit 9 | В | О'):
            self.assertIn('Ошибка:',self.command(command,True))

    def test_faq_capacity_and_space_reclaimed(self):
        for i in range(100):self.command(f'/faqadd Вопрос {i} | Ответ',True)
        self.assertIn('лимит 100',self.command('/faqadd Лишний | Ответ',True))
        self.command('/faqdel 1',True)
        self.assertIn('#101',self.command('/faqadd Освободилось | Ответ',True))

    def test_search_in_answers_casefold_and_bounded_results(self):
        for i in range(12):self.command(f'/faqadd Вопрос {i} | Приём заявок',True)
        result=self.command('/faqfind ПРИЕМ')
        self.assertIn('вопросов: 12',result);self.assertIn('Показаны первые 10',result)
        self.assertNotIn('#11.',result)
        self.assertIn('Ничего не найдено',self.command('/faqfind отсутствует'))
        for query in ('','x'*81):self.assertIn('Ошибка:',self.command('/faqfind '+query))

    def test_reply_size_does_not_truncate_maximum_faq_answer(self):
        answer='x'*1600
        self.command('/faqadd '+'в'*180+' | '+answer,True)
        result=self.command('/answer 1')
        self.assertTrue(result.endswith(answer));self.assertLess(len(result),3500)

    def test_chat_and_platform_settings_are_independent(self):
        self.command('/faqadd Местный | Ответ',True)
        self.command('/welcomeon',True)
        self.command('/setwelcome Наш чат',True)
        for platform,chat in (('telegram','-100'),('vk','-100'),('max','-200')):
            self.assertIn('пуст',self.command('/faq',platform=platform,chat=chat))
            self.assertIn('выключено',self.command('/welcome',platform=platform,chat=chat))

    def test_settings_and_faq_survive_restart(self):
        self.command('/faqadd Сохранено | Ответ',True);self.command('/welcomeon',True)
        self.service.db.close();self.service=Service(self.path);self.addCleanup(self.service.db.close)
        self.assertIn('Сохранено',self.command('/answer 1'))
        self.assertEqual(DEFAULT_WELCOME,self.command('',event_id='join1',kind='join'))

    def test_welcome_text_and_enable_disable(self):
        self.assertIn('выключено',self.command('/welcome'))
        self.command('/setwelcome Привет! /rules /faq',True)
        self.assertIn('выключено',self.command('/welcome'))
        self.assertIsNone(self.command('',event_id='j1',kind='join'))
        self.command('/welcomeon',True)
        self.assertEqual('Привет! /rules /faq',self.command('',event_id='j2',kind='join'))
        self.command('/welcomeoff',True)
        self.assertIsNone(self.command('',event_id='j3',kind='join'))
        for cmd in ('/setwelcome','/setwelcome '+'x'*2001,'/welcomeon yes'):
            self.assertIn('Ошибка:',self.command(cmd,True))

    def test_disabled_join_is_deduplicated_without_outbox(self):
        self.assertIsNone(self.command('',event_id='j1',kind='join',enqueue_reply=True))
        self.command('/welcomeon',True)
        self.assertIsNone(self.command('',event_id='j1',kind='join',enqueue_reply=True))
        self.assertEqual(0,self.service.db.execute('SELECT count(*) FROM outbox').fetchone()[0])

    def test_join_never_records_agreement_or_executes_text(self):
        self.command('/setrules Правила',True);self.command('/welcomeon',True)
        self.command('/agree',event_id='j1',kind='join')
        self.assertNotIn('agreements',self.state())
        self.assertIn('Нужно',self.command('/mine'))
        self.assertIn('Неизвестная',self.command('/join'))
        with self.assertRaises(ValueError):self.command('',kind='join')

    def test_real_normalizers_route_joins_on_each_platform_once(self):
        for transport,make,chat in self.adapters():
            transport.send=Mock()
            self.command('/welcomeon',True,platform=transport.platform,chat=chat)
            groups={chat:{'admins':['99']}}
            for _ in range(2):process_update(self.service,transport,groups,make())
            transport.send.assert_called_once()
            self.assertEqual(DEFAULT_WELCOME,transport.send.call_args.args[1])
            self.assertEqual('42',transport.send.call_args.args[0].user)

    def test_unlisted_chats_and_disabled_greetings_send_nothing(self):
        for transport,make,chat in self.adapters():
            transport.send=Mock()
            process_update(self.service,transport,{},make())
            process_update(self.service,transport,{chat:{'admins':['99']}},make())
            transport.send.assert_not_called()

    def test_telegram_batch_ignores_bots_and_private_chats(self):
        tg=Telegram('test');tg.username='ours'
        update=tg_join();update['message']['new_chat_members']=[{'id':5,'is_bot':True},{'id':42,'is_bot':False},{'id':43,'is_bot':False}]
        self.assertEqual(Event('-100','42','','101','join'),tg.normalize(update))
        for mutate in (lambda u:u['message'].update(new_chat_members=[{'id':5,'is_bot':True}]),
                       lambda u:u['message']['chat'].update(type='private'),
                       lambda u:u.update(update_id=True),lambda u:u.update(update_id=-1),
                       lambda u:u['message'].update(new_chat_members=[{'id':True,'is_bot':False}])):
            bad=copy.deepcopy(update);mutate(bad);self.assertIsNone(tg.normalize(bad))

    def test_max_join_identity_and_strict_channel_bot_checks(self):
        transport=Max('test');event=transport.normalize(max_join())
        self.assertEqual('join',event.kind)
        self.assertNotEqual(event.id,transport.normalize(max_join(stamp=1794474000001)).id)
        self.assertNotEqual(event.id,transport.normalize(max_join(uid=43)).id)
        for changes in ({'is_channel':True},{'is_channel':None},{'timestamp':True},{'timestamp':-1},
                        {'user':{'user_id':42,'is_bot':True}},{'user':None},{'chat_id':0}):
            update=max_join();update.update(changes);self.assertIsNone(transport.normalize(update))

    def test_vk_requires_member_and_valid_conversation_event_id(self):
        transport=Vk('test',123)
        by_link=vk_join();by_link['object']['message']['action']['type']='chat_invite_user_by_link'
        self.assertEqual('join',transport.normalize(by_link).kind)
        for changes in ({'peer_id':42},{'conversation_message_id':None},
                        {'action':{'type':'chat_invite_user'}},
                        {'action':{'type':'chat_invite_user','member_id':-123}},
                        {'action':{'type':'chat_kick_user','member_id':42}},{'out':1}):
            update=vk_join();update['object']['message'].update(changes)
            self.assertIsNone(transport.normalize(update))
        wrong=vk_join();wrong['group_id']=999;self.assertIsNone(transport.normalize(wrong))

    def test_max_webhook_join_is_durable_and_deduplicated(self):
        secret='x'*40;groups={'-100':{'admins':['99']}}
        app=WebhookApp(self.inbox,groups,secret)
        self.command('/welcomeon',True)
        data=json.dumps(max_join()).encode();status=[]
        def deliver():
            env={'PATH_INFO':'/webhooks/max','REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json',
                 'CONTENT_LENGTH':str(len(data)),'HTTP_X_MAX_BOT_API_SECRET':secret,'wsgi.input':io.BytesIO(data)}
            b''.join(app(env,lambda s,h:status.append(s)))
        deliver();deliver()
        self.assertEqual(['200 OK','200 OK'],status)
        row=self.service.db.execute('SELECT event FROM webhook_inbox').fetchone()
        self.assertEqual('join',json.loads(row[0])['kind'])
        self.service.db.close();self.service=Service(self.path);self.addCleanup(self.service.db.close)
        transport=Mock()
        worker_tick(self.service,transport,self.inbox,groups)
        deliver();worker_tick(self.service,transport,self.inbox,groups)
        transport.send.assert_called_once()
        self.assertEqual(DEFAULT_WELCOME,transport.send.call_args.args[1])

    def test_legacy_queue_event_without_kind_remains_command(self):
        payload={'chat':'-100','user':'42','text':'/faq','id':'legacy'}
        event=Event(**payload)
        self.assertEqual('command',event.kind)
        self.assertIn('пуст',self.service.execute('max',event.chat,event.user,event.text,event_id=event.id,kind=event.kind))


if __name__ == '__main__':unittest.main()
