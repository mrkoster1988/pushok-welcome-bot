"""Лимит команд на пользователя; счётчик фиксируется в транзакции команды."""
import json

DEFAULT_LIMIT = 20
WINDOW = 60
MAX_USERS = 1000


def command_allowed(db, scope, user, limit, now):
    if limit == 0:
        return True,None
    key = 'guard:' + scope
    row = db.execute('SELECT value FROM transport_meta WHERE key=?',(key,)).fetchone()
    users = json.loads(row[0]) if row else {}
    users = {u:v for u,v in users.items() if 0 <= now-v['start'] < WINDOW}
    if user not in users and len(users) >= MAX_USERS:
        return False,None
    counter = users.setdefault(user,{'start':now,'count':0,'warned':False})
    counter['count'] = min(counter['count']+1,121)
    allowed = counter['count'] <= limit
    reply = None
    if not allowed and not counter['warned']:
        reply = 'Слишком много команд. Подождите до минуты и попробуйте снова.'
        counter['warned'] = True
    db.execute('INSERT OR REPLACE INTO transport_meta VALUES (?,?)',(key,json.dumps(users)))
    return allowed,reply
