"""Черновик настройки одного чата: владелец, срок жизни и проверка конфликта."""
import hashlib
import json
import re

FIELDS = ('rules','version','faq','faq_next_id','welcome_text','welcome_enabled','command_limit','welcome_cooldown')
STEPS = [('rules','Введите правила: /step текст. Пропустить: /skip'),
         ('welcome_text','Введите приветствие: /step текст. Пропустить: /skip'),
         ('faq','Добавьте один вопрос: /step вопрос | ответ. Пропустить: /skip'),
         ('welcome_enabled','Включать приветствие? /step да или /step нет. Пропустить: /skip'),
         ('command_limit','Команд участника за минуту (0–120): /step 20. Пропустить: /skip'),
         ('welcome_cooldown','Пауза приветствий (0–86400 секунд): /step 30. Пропустить: /skip')]
COMMANDS = {'/wizard','/step','/skip','/back','/cancel','/apply'}
TTL = 1800


def fingerprint(state):
    return hashlib.sha256(json.dumps({k:state.get(k) for k in FIELDS},sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def prompt(session):
    i=session['index']
    if i<len(STEPS):return f'Настройка {i+1}/{len(STEPS)}\n'+STEPS[i][1]+'\nНазад: /back. Отмена: /cancel.'
    draft=session['draft'];lines=['Проверьте черновик (ещё не применён):']
    for key,label in [('rules','Правила'),('welcome_text','Приветствие'),('faq','Вопрос FAQ'),('welcome_enabled','Включение'),('command_limit','Лимит команд'),('welcome_cooldown','Пауза приветствий')]:
        value=draft.get(key,'без изменения')
        if key=='faq' and isinstance(value,dict):value=value['question']+' | '+value['answer']
        if isinstance(value,bool):value='да' if value else 'нет'
        lines.append(label+': '+str(value)[:350])
    return '\n'.join(lines)+'\nСохранить: /apply. Назад: /back. Отмена: /cancel.\nДлинные тексты показаны сокращённо; /draft rules или /draft welcome_text или /draft faq — полный текст.'


def handle(state,user,cmd,arg,admin,now):
    from core import require_admin
    from domain import faq_pair
    require_admin(admin)
    session=state.get('_wizard')
    expired=session is not None and not 0<=now-session['started']<TTL
    if cmd=='/wizard':
        if arg:raise ValueError('Начало или продолжение: /wizard без параметров.')
        if session and not expired:
            if session['owner']!=user:raise ValueError('Настройку ведёт другой администратор. Черновик действует 30 минут.')
            return prompt(session)
        session={'owner':user,'started':now,'index':0,'draft':{},'base':fingerprint(state)}
        state['_wizard']=session
        return prompt(session)
    if not session or expired:raise ValueError('Нет действующего черновика. Начните /wizard; срок — 30 минут.')
    if session['owner']!=user:raise ValueError('Этот черновик принадлежит другому администратору.')
    if cmd=='/cancel':
        if arg:raise ValueError('/cancel не принимает параметры.')
        del state['_wizard'];return 'Черновик отменён. Настройки не менялись.'
    if cmd=='/draft':
        if arg not in ('rules','welcome_text','faq'):raise ValueError('/draft rules, /draft welcome_text или /draft faq')
        value=session['draft'].get(arg)
        if value is None:return 'Это поле оставлено без изменения.'
        return value['question']+'\n\n'+value['answer'] if isinstance(value,dict) else value
    if cmd=='/back':
        if arg:raise ValueError('/back не принимает параметры.')
        session['index']=max(0,session['index']-1);return prompt(session)
    if cmd=='/apply':
        if arg:raise ValueError('/apply не принимает параметры.')
        if session['index']!=len(STEPS):raise ValueError('Сначала пройдите шаги мастера.')
        if fingerprint(state)!=session['base']:raise ValueError('Настройки изменились во время мастера. Отмените /cancel и начните /wizard заново.')
        draft=session['draft']
        if 'faq' in draft and len(state.get('faq',{}))>=100:raise ValueError('Достигнут лимит 100 вопросов.')
        if 'rules' in draft and draft['rules']!=state.get('rules'):
            state['rules']=draft['rules'];state['version']=state.get('version',0)+1
        for key in ('welcome_text','welcome_enabled','command_limit','welcome_cooldown'):
            if key in draft:state[key]=draft[key]
        if 'faq' in draft:
            key=str(state.get('faq_next_id',1));state.setdefault('faq',{})[key]=draft['faq'];state['faq_next_id']=int(key)+1
        del state['_wizard'];return 'Настройки сохранены. Проверьте /setup, /rules, /faq и /welcome.'
    i=session['index']
    if i>=len(STEPS):raise ValueError('Черновик готов: /apply, /back или /cancel.')
    key=STEPS[i][0]
    if cmd=='/skip':
        if arg:raise ValueError('/skip не принимает параметры.')
        session['draft'].pop(key,None)
    else:
        value=arg
        if key in ('rules','welcome_text'):
            maximum=2000 if key=='welcome_text' else 2800
            if not value or len(value)>maximum:raise ValueError(f'Нужен текст от 1 до {maximum} символов после /step.')
        elif key=='faq':
            if '|' not in value:raise ValueError('/step вопрос | ответ')
            value=faq_pair(*value.split('|',1))
        elif key=='welcome_enabled':
            if value.casefold() not in ('да','нет'):raise ValueError('/step да или /step нет')
            value=value.casefold()=='да'
        else:
            maximum=120 if key=='command_limit' else 86400
            if not re.fullmatch(r'0|[1-9][0-9]{0,4}',value) or int(value)>maximum:raise ValueError(f'Укажите число от 0 до {maximum}.')
            value=int(value)
        session['draft'][key]=value
    session['index']+=1;return prompt(session)
