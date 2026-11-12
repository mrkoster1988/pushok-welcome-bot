"""Постоянная очередь ответов. Неизвестный исход отправки не повторяется автоматически."""
import argparse
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from maintenance import inspect_db, lock_database, meta, PROJECT_KEY, project_id, readonly, require_active_db
from transports import Event

PLATFORMS = ('telegram', 'max', 'vk')


def recover_interrupted_sends(service, platform='max'):
    # Вызывать только при старте единственного обработчика, под блокировкой базы.
    with service.db:
        return service.db.execute("UPDATE outbox SET status='uncertain' WHERE platform=? AND status='sending'",
                                  (platform,)).rowcount


def send_next(service, transport, groups, platform=None):
    platform = platform or transport.platform
    with service.db:
        service.db.execute('BEGIN IMMEDIATE')
        row = service.db.execute("SELECT id,chat,user,reply FROM outbox WHERE platform=? AND status='pending' ORDER BY rowid LIMIT 1",
                                 (platform,)).fetchone()
        if row is None:
            return None
        event_id, chat, user, text = row
        state = 'sending' if chat in groups else 'cancelled'
        service.db.execute('UPDATE outbox SET status=? WHERE platform=? AND id=?', (state,platform,event_id))
    if state == 'sending':
        try:
            transport.send(Event(chat,user,'',event_id),text)
            state = 'sent'
        except Exception:
            state = 'uncertain'
            logging.error('%s: отправка не подтверждена; автоматического повтора нет. Проверьте delivery.py status.',platform)
        # Авария до этого COMMIT оставит sending; при старте он станет uncertain.
        with service.db:
            service.db.execute('UPDATE outbox SET status=? WHERE platform=? AND id=?', (state,platform,event_id))
    return state


def drain(service, transport, groups, limit=50):
    handled = 0
    while handled < limit:
        state = send_next(service,transport,groups)
        if state is None:
            break
        handled += 1
        if state == 'uncertain':
            break
    return handled


def status(db, platform, details=False):
    counts = dict(db.execute('SELECT status,count(*) FROM outbox WHERE platform=? GROUP BY status',(platform,)))
    result = {'platform':platform,'outbox':counts,'events':db.execute('SELECT count(*) FROM events WHERE platform=?',(platform,)).fetchone()[0]}
    if details:
        result['unresolved'] = [dict(zip(('event_id','chat','status'),row)) for row in db.execute(
            "SELECT id,chat,status FROM outbox WHERE platform=? AND status IN ('pending','sending','uncertain','held') ORDER BY rowid LIMIT 100",(platform,))]
        result['details_limit'] = 100
    return result


def resolve(db, platform, event_id, decision, apply=False):
    if decision not in ('sent','cancelled'):
        raise ValueError('Допустимо sent или cancelled; повторная отправка этой командой невозможна.')
    row = db.execute('SELECT status FROM outbox WHERE platform=? AND id=?',(platform,event_id)).fetchone()
    if row is None or row[0] != 'uncertain':
        raise ValueError('Нужен ID ответа со статусом uncertain на выбранной платформе.')
    if apply:
        changed = db.execute("UPDATE outbox SET status=? WHERE platform=? AND id=? AND status='uncertain'",
                             (decision,platform,event_id)).rowcount
        if changed != 1:
            raise ValueError('Статус изменился. Повторите проверку.')
    return {'event_id':event_id,'from':'uncertain','to':decision,'applied':apply,'sends':0}


def prune(db, platform, keep_last=1000, apply=False):
    if type(keep_last) is not int or not 0 <= keep_last <= 1000000:
        raise ValueError('keep-last: целое число от 0 до 1000000.')
    # Удаляем только завершённые ответы. ID в events сохраняются для защиты от дублей.
    query = "SELECT rowid FROM outbox WHERE platform=? AND status IN ('sent','cancelled') ORDER BY rowid DESC LIMIT -1 OFFSET ?"
    count = db.execute('SELECT count(*) FROM ('+query+')',(platform,keep_last)).fetchone()[0]
    if apply:
        db.execute('DELETE FROM outbox WHERE rowid IN ('+query+')',(platform,keep_last))
    return {'platform':platform,'eligible':count,'keep_last':keep_last,'applied':apply,'event_ids_preserved':True}


def check_database(db):
    inspect_db(db)
    if meta(db,PROJECT_KEY) != project_id():
        raise ValueError('Нужна база этого проекта, запущенная после alpha.4.')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone():
        raise ValueError('В базе ещё нет очереди ответов.')


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--platform',choices=PLATFORMS,required=True)
    commands = parser.add_subparsers(dest='mode',required=True)
    p = commands.add_parser('status');p.add_argument('--details',action='store_true')
    p = commands.add_parser('resolve');p.add_argument('--event-id',required=True);p.add_argument('--as',dest='decision',choices=('sent','cancelled'),required=True);p.add_argument('--apply',action='store_true')
    p = commands.add_parser('prune');p.add_argument('--keep-last',type=int,default=1000);p.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    path = Path(args.db).absolute()
    def operation(db):
        if args.mode == 'status':return status(db,args.platform,args.details)
        if args.mode == 'resolve':return resolve(db,args.platform,args.event_id,args.decision,args.apply)
        return prune(db,args.platform,args.keep_last,args.apply)
    with closing(readonly(path)) as db:
        check_database(db)
        if not getattr(args,'apply',False):
            print(json.dumps(operation(db),ensure_ascii=False,indent=2))
            return
    with lock_database(path), closing(readonly(path)) as check:
        check_database(check)
        with closing(sqlite3.connect(path)) as db, db:
            require_active_db(db)
            db.execute('BEGIN IMMEDIATE')
            result = operation(db)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        cli()
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError,sqlite3.Error):
        raise SystemExit('Операция не выполнена: проверьте путь, права и базу.') from None
