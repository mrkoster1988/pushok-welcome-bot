"""Правила, FAQ и приветствие участников: настройки изолированы по чату и платформе."""
import re
from guard import DEFAULT_LIMIT

HELP = '''/menu — меню с кнопками
/rules — правила
/agree — принять текущие правила
/mine — моё согласие
/faq [страница] — список вопросов
/answer ID — ответ на вопрос
/faqfind текст — найти вопрос или ответ
/welcome — состояние и текст приветствия

Администратор из groups.json:
/setrules текст — изменить правила
/faqadd вопрос | ответ — добавить FAQ
/faqedit ID | вопрос | ответ — изменить FAQ
/faqdel ID — удалить FAQ
/setwelcome текст — изменить приветствие
/welcomeon — включить автоприветствие
/welcomeoff — выключить автоприветствие
/welcomecooldown секунды — пауза между приветствиями
/setlimit число — команд участника за минуту, 0 отключает
/setup — проверить настройки чата
/wizard — пошаговая настройка
/step текст, /skip, /back, /apply, /cancel — команды мастера'''
DEFAULT_WELCOME = 'Добро пожаловать в группу!\nПравила: /rules\nЧастые вопросы: /faq\nПосле прочтения правил: /agree'
PAGE_SIZE = 5
FAQ_LIMIT = 100


def identifier(text):
    if not re.fullmatch(r'[1-9][0-9]{0,8}',text):
        raise ValueError('Укажите положительный числовой ID или номер страницы.')
    return str(int(text))


def faq_pair(question, answer):
    question,answer = question.strip(),answer.strip()
    if not question or len(question)>180 or '\n' in question or '\r' in question:
        raise ValueError('Вопрос: одна строка от 1 до 180 символов.')
    if not answer or len(answer)>1600:
        raise ValueError('Ответ: от 1 до 1600 символов.')
    return {'question':question,'answer':answer}


def faq_item(state, key):
    key = identifier(key)
    if key not in state.get('faq',{}):
        raise ValueError('Вопрос не найден. Откройте /faq.')
    return key,state['faq'][key]


def welcome_text(state):
    return state.get('welcome_text',DEFAULT_WELCOME)


def handle_join(state, now):
    # Никакого автоматического согласия с правилами или выдачи прав при вступлении.
    if state.get('welcome_enabled') is not True:
        return None
    previous = state.get('_welcome_last_at')
    cooldown = state.get('welcome_cooldown',30)
    if previous is not None and 0 <= now-previous < cooldown:
        return None
    state['_welcome_last_at'] = now
    return welcome_text(state)


def handle(s, u, cmd, arg, admin, now=None):
    from core import require_admin
    from wizard import COMMANDS,handle as wizard_handle

    if cmd in COMMANDS or cmd=='/draft':
        return wizard_handle(s,u,cmd,arg,admin,now)
    if cmd=='/menu':return 'Выберите действие кнопками под сообщением. Команды также доступны: /help.'

    if cmd == '/setup':
        require_admin(admin)
        rules = f"заданы, версия {s['version']}" if 'rules' in s else 'не заданы → /setrules текст'
        faq = f"{len(s.get('faq',{}))} вопросов" if s.get('faq') else 'пуст → /faqadd вопрос | ответ'
        welcome = 'включено' if s.get('welcome_enabled') is True else 'выключено → /welcomeon'
        limit = s.get('command_limit',DEFAULT_LIMIT)
        return (f'Настройки текущего чата\nПравила: {rules}\nFAQ: {faq}\nПриветствие: {welcome}'
                f"\nПауза приветствий: {s.get('welcome_cooldown',30)} с → /welcomecooldown число"
                f'\nЛимит команд участника: {limit} за минуту (0 — выключен) → /setlimit число'
                '\nТекст приветствия: /welcome\nФактическую доставку проверьте в группе.')
    if cmd in ('/setlimit','/welcomecooldown'):
        require_admin(admin)
        maximum = 120 if cmd == '/setlimit' else 86400
        if not re.fullmatch(r'0|[1-9][0-9]{0,4}',arg) or int(arg)>maximum:
            raise ValueError(f'Укажите целое число от 0 до {maximum}.')
        if cmd == '/setlimit':
            s['command_limit'] = int(arg)
            return f'Лимит команд участника: {arg} за минуту. 0 отключает ограничение. Администраторы освобождены.'
        s['welcome_cooldown'] = int(arg)
        return f'Пауза между приветствиями: {arg} с. Вступления внутри паузы пропускаются без отложенной отправки.'
    if cmd == '/setrules':
        require_admin(admin)
        if not arg: raise ValueError('Добавьте текст правил.')
        s['rules'] = arg
        s['version'] = s.get('version',0)+1
        return 'Правила обновлены.'
    if cmd == '/rules': return s.get('rules','Администратор ещё не добавил правила.')
    if cmd == '/agree':
        if 'rules' not in s: raise ValueError('Правила ещё не заданы.')
        s.setdefault('agreements',{})[u] = s['version']
        return 'Согласие записано.'
    if cmd == '/mine':
        accepted = s.get('agreements',{}).get(u)
        return 'Актуальные правила приняты.' if accepted and accepted==s.get('version') else 'Нужно принять текущие правила.'

    if cmd == '/faqadd':
        require_admin(admin)
        if '|' not in arg: raise ValueError('Используйте /faqadd вопрос | ответ.')
        entry = faq_pair(*arg.split('|',1))
        if len(s.get('faq',{})) >= FAQ_LIMIT: raise ValueError('Достигнут лимит 100 вопросов. Удалите ненужный вопрос.')
        key = str(s.get('faq_next_id',1))
        s.setdefault('faq',{})[key] = entry
        s['faq_next_id'] = int(key)+1
        return f'Вопрос #{key} добавлен. /answer {key}'
    if cmd == '/faqedit':
        require_admin(admin)
        values = [value.strip() for value in arg.split('|',2)]
        if len(values)!=3: raise ValueError('Используйте /faqedit ID | вопрос | ответ.')
        key,_ = faq_item(s,values[0])
        s['faq'][key] = faq_pair(values[1],values[2])
        return f'Вопрос #{key} обновлён.'
    if cmd == '/faqdel':
        require_admin(admin)
        key,_ = faq_item(s,arg)
        del s['faq'][key]
        return f'Вопрос #{key} удалён.'
    if cmd == '/answer':
        key,entry = faq_item(s,arg)
        return f"#{key}. {entry['question']}\n\n{entry['answer']}"
    if cmd == '/faq':
        page = int(identifier(arg or '1'))
        entries = sorted(s.get('faq',{}).items(),key=lambda pair:int(pair[0]))
        pages = max(1,(len(entries)+PAGE_SIZE-1)//PAGE_SIZE)
        if page>pages: raise ValueError(f'Такой страницы нет. Страниц: {pages}.')
        if not entries: return 'FAQ пока пуст. Администратор может добавить вопрос: /faqadd вопрос | ответ'
        rows = [f"#{key}. {entry['question']}" for key,entry in entries[(page-1)*PAGE_SIZE:page*PAGE_SIZE]]
        result = f'FAQ — страница {page}/{pages}\n'+'\n'.join(rows)+'\nОтвет: /answer ID'
        if page<pages: result += f'\nДалее: /faq {page+1}'
        return result
    if cmd == '/faqfind':
        if not arg or len(arg)>80: raise ValueError('Введите запрос от 1 до 80 символов: /faqfind текст')
        def folded(text): return text.casefold().replace('ё','е')
        needle = folded(arg)
        found = [(key,entry) for key,entry in sorted(s.get('faq',{}).items(),key=lambda pair:int(pair[0]))
                 if needle in folded(entry['question']+'\n'+entry['answer'])]
        if not found: return 'Ничего не найдено. Попробуйте другое слово или /faq.'
        result = f'Найдено вопросов: {len(found)}\n'+'\n'.join(f"#{key}. {entry['question']}" for key,entry in found[:10])+'\nОтвет: /answer ID'
        if len(found)>10: result += '\nПоказаны первые 10. Уточните запрос.'
        return result

    if cmd == '/setwelcome':
        require_admin(admin)
        if not arg or len(arg)>2000: raise ValueError('Приветствие: от 1 до 2000 символов.')
        s['welcome_text'] = arg
        return 'Текст приветствия сохранён. Для включения: /welcomeon'
    if cmd in ('/welcomeon','/welcomeoff'):
        require_admin(admin)
        if arg: raise ValueError('Эта команда не принимает параметры.')
        s['welcome_enabled'] = cmd=='/welcomeon'
        return 'Автоприветствие включено.' if s['welcome_enabled'] else 'Автоприветствие выключено.'
    if cmd == '/welcome':
        status = 'включено' if s.get('welcome_enabled') is True else 'выключено'
        return f'Автоприветствие: {status}\n\n'+welcome_text(s)
