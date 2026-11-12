"""Локальный HTTP smoke реального Gunicorn, без запросов в MAX."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from unittest.mock import Mock

ROOT = Path.cwd()
from core import Service
from main import bind_identity, metadata
from max_webhook import Inbox, worker_tick
def max_update():
    return {'update_type':'message_created','message':{'sender':{'user_id':42,'is_bot':False},
            'recipient':{'chat_id':-100,'chat_type':'chat'},'body':{'mid':'http.command','text':'/help'}}}


def max_join():
    return {'update_type':'user_added','chat_id':-100,'timestamp':1794474000000,'is_channel':False,
            'user':{'user_id':43,'is_bot':False},'inviter_id':42}

from domain import DEFAULT_WELCOME

SECRET = 'local-http-test-' + 'x'*32
with tempfile.TemporaryDirectory() as tmp:
    temp = Path(tmp)
    path = str(temp/'smoke.sqlite3')
    service = Service(path)
    bind_identity(service,'max','123')
    metadata(service,'max:webhook_heartbeat',time.time(),write=True)
    inbox = Inbox(path)
    (temp/'fixture.py').write_text('from max_webhook import Inbox, WebhookApp\nimport os\napp = WebhookApp(Inbox(os.environ["SMOKE_DB"]), {"-100":{"admins":["42"]}}, os.environ["SMOKE_SECRET"])\n')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    env = {**os.environ,'SMOKE_DB':path,'SMOKE_SECRET':SECRET,'PYTHONPATH':str(ROOT)+os.pathsep+str(temp)}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(payload=None, secret=SECRET):
        target = '/healthz' if payload is None else '/webhooks/max'
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{port}'+target, data=data,headers={'Content-Type':'application/json','X-Max-Bot-Api-Secret':secret})
        try:
            with opener.open(req,timeout=3) as res:return res.status,json.load(res)
        except urllib.error.HTTPError as err:return err.code,json.load(err)
    def start():
        proc = subprocess.Popen([sys.executable,'-m','gunicorn','fixture:app','--bind',f'127.0.0.1:{port}','--workers','2','--timeout','25'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError(proc.communicate()[1].decode())
            try:
                if request()[0]==200:return proc
            except (OSError,urllib.error.URLError):pass
            time.sleep(.05)
        proc.terminate()
        proc.wait(timeout=5)
        raise RuntimeError('Gunicorn did not become ready')
    def stop(proc):
        proc.terminate()
        proc.communicate(timeout=10)
        assert proc.returncode==0,proc.returncode
    proc=start()
    try:
        assert request(max_update(),secret='wrong')[0]==403
        assert request(max_update())[0]==200
        assert request(max_update())[0]==200
        assert inbox.status()['inbox']=={'pending':1}
    finally:stop(proc)
    proc=start()
    try:
        assert inbox.status()['inbox']=={'pending':1}
        transport=Mock()
        worker_tick(service,transport,inbox,{'-100':{'admins':['42']}})
        transport.send.assert_called_once()
        assert request(max_update())[0]==200
        worker_tick(service,transport,inbox,{'-100':{'admins':['42']}})
        transport.send.assert_called_once()
        assert inbox.status()['outbox']=={'sent':1}
        service.execute('max','-100','42','/welcomeon',True)
        assert request(max_join())[0]==200
        assert request(max_join())[0]==200
        assert inbox.status()['inbox']=={'done':1,'pending':1}
    finally:stop(proc)
    proc=start()
    try:
        worker_tick(service,transport,inbox,{'-100':{'admins':['42']}})
        assert transport.send.call_count==2
        assert transport.send.call_args.args[1]==DEFAULT_WELCOME
        assert request(max_join())[0]==200
        worker_tick(service,transport,inbox,{'-100':{'admins':['42']}})
        assert transport.send.call_count==2
        assert inbox.status()['outbox']=={'sent':2}
        print('PASS: Gunicorn, 2 workers, HTTP 403/200, command + join, durable queue, two restarts, duplicate suppression; API sends mocked')
    finally:
        stop(proc)
        service.db.close()
