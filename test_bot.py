import json
import tempfile
import unittest
from pathlib import Path
from core import Service
from main import normalize

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.service = Service(':memory:')
    def tearDown(self):
        self.service.db.close()
    def test_scenario(self):
        for command, admin, expected in json.loads(Path(__file__).with_name('scenario.json').read_text()):
            with self.subTest(command=command):
                result=self.service.execute('telegram','chat','alice',command,admin)
                self.assertIn(expected,result)
    def test_chat_and_platform_isolation(self):
        command, admin, expected = json.loads(Path(__file__).with_name('scenario.json').read_text())[0]
        self.service.execute('telegram','a','alice',command,admin)
        self.service.execute('telegram','b','alice','/help')
        self.service.execute('vk','a','alice','/help')
        rows=dict(self.service.db.execute('SELECT scope,data FROM chats').fetchall())
        self.assertEqual('{}',rows[json.dumps(['telegram','b'])])
        self.assertEqual('{}',rows[json.dumps(['vk','a'])])
    def test_duplicate_event(self):
        command, admin, _ = json.loads(Path(__file__).with_name('scenario.json').read_text())[0]
        first=self.service.execute('telegram','a','alice',command,admin,'100')
        self.assertIsNotNone(first)
        before=self.service.db.execute('SELECT data FROM chats').fetchall()
        self.assertIsNone(self.service.execute('telegram','a','alice',command,admin,'100'))
        self.assertEqual(before,self.service.db.execute('SELECT data FROM chats').fetchall())
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'bot.db')
            one=Service(path)
            command,admin,_=json.loads(Path(__file__).with_name('scenario.json').read_text())[0]
            one.execute('telegram','a','alice',command,admin,'101')
            before=one.db.execute('SELECT data FROM chats').fetchall()
            one.db.close()
            two=Service(path)
            self.assertEqual(before,two.db.execute('SELECT data FROM chats').fetchall())
            self.assertIsNone(two.execute('telegram','a','alice',command,admin,'101'))
            two.db.close()
    def test_admin_denied(self):
        scenarios=json.loads(Path(__file__).with_name('scenario.json').read_text())
        admin_commands=[row for row in scenarios if row[1]]
        for command,_,_ in admin_commands:
            self.assertIn('Ошибка:',self.service.execute('telegram','x','stranger',command))
    def test_telegram_addressing(self):
        event={'message':{'chat':{'id':-1},'from':{'id':2},'text':'/help@ourbot'}}
        self.assertEqual(('-1','2','/help'),normalize(event,'ourbot'))
        self.assertIsNone(normalize(event,'otherbot'))
        event['message']['sender_chat']={'id':-1}
        self.assertIsNone(normalize(event,'ourbot'))
    def test_invalid_input(self):
        self.assertIn('3000',self.service.execute('telegram','a','x','x'*3001))
        self.assertIn('Неизвестная',self.service.execute('telegram','a','x','/invalid'))

if __name__=='__main__': unittest.main()
