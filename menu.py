"""Небольшое русское меню. Кнопки не заменяют проверку прав в сервисе."""
import json

BUTTONS = [('Правила','/rules'),('Вопросы','/faq'),('Мой статус','/mine'),
           ('Помощь','/help'),('Настройка','/wizard')]
PAYLOADS = {'pushok:'+command[1:]:command for _,command in BUTTONS}
LABELS = dict(BUTTONS)


def callback_command(value):
    return PAYLOADS.get(value) if isinstance(value,str) else None


def label_command(value):
    return LABELS.get(value) if isinstance(value,str) else None


def rows(values):
    return [values[i:i+2] for i in range(0,len(values),2)]


def telegram_keyboard():
    return {'inline_keyboard':rows([{'text':label,'callback_data':'pushok:'+cmd[1:]} for label,cmd in BUTTONS])}


def max_keyboard():
    return [{'type':'inline_keyboard','payload':{'buttons':rows([{'type':'message','text':label} for label,_ in BUTTONS])}}]


def vk_keyboard():
    return json.dumps({'inline':True,'one_time':False,'buttons':rows([
        {'action':{'type':'text','label':label},'color':'default'} for label,_ in BUTTONS])},ensure_ascii=False)
