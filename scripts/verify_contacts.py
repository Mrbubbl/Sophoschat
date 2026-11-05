"""Disposable real HTTP acceptance server for contacts; no paid model calls.

Use --keep-running for browser checks. Random test credentials remain in ignored
output/. The server binds only localhost and never reads production data.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parent.parent


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--out',type=Path,default=ROOT/'output/contacts-acceptance')
    p.add_argument('--keep-running',action='store_true')
    args=p.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    data=out/('data-'+secrets.token_hex(4));data.mkdir()
    owner={'username':'contact_owner','password':secrets.token_urlsafe(18)}
    alice={'username':'contact_alice','password':secrets.token_urlsafe(18)}
    bob={'username':'contact_bob','password':secrets.token_urlsafe(18)}
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    url=f'http://127.0.0.1:{port}'
    env={**os.environ,'AICHAT_DATA_DIR':str(data),'PYTHONPATH':str(ROOT),
         'CONTACT_TEST_OWNER':json.dumps(owner),'CONTACT_TEST_PORT':str(port)}
    code='''import os,json,uvicorn
from aichat.main import create_app
from aichat import auth,chat
app=create_app(scheduler=False,start_agent_service=False)
u=json.loads(os.environ['CONTACT_TEST_OWNER'])
uid=auth.create_user(u['username'],'联系人验收 Owner',u['password'],'owner')
chat.bootstrap_user(uid)
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['CONTACT_TEST_PORT']),log_level='warning')
'''
    with (out/'server.log').open('w') as log:
        server=subprocess.Popen([sys.executable,'-c',code],cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
    try:
        with httpx.Client(base_url=url,trust_env=False,timeout=20) as c, httpx.Client(base_url=url,trust_env=False,timeout=20) as a, httpx.Client(base_url=url,trust_env=False,timeout=20) as b:
            for _ in range(100):
                try:
                    if c.post('/api/login',json=owner).status_code==200:break
                except httpx.HTTPError:pass
                if server.poll() is not None:raise RuntimeError('server_exited')
                time.sleep(.1)
            else:raise RuntimeError('server_not_ready')
            r=a.post('/api/register',json={**alice,'display_name':'测试成员甲'});r.raise_for_status();aid=r.json()['user']['id']
            r=b.post('/api/register',json={**bob,'display_name':'测试成员乙'});r.raise_for_status();bid=r.json()['user']['id']
            assert [r['type'] for r in a.get('/api/rooms').json()['rooms']]==['agent_dm']
            assert a.get('/api/contacts').json()['contacts']==[]
            assert a.get('/api/users').json()['users']==[]
            assert a.get('/api/admin/users').status_code==403
            # The owner exercises HTTP independently, leaving Alice's list empty for UI.
            assert c.get('/api/users/lookup',params={'username':' CONTACT_BOB '}).json()['user']['id']==bid
            for _ in range(2):c.post('/api/contacts',json={'user_id':bid}).raise_for_status()
            assert len(c.get('/api/contacts').json()['contacts'])==1
            assert b.get('/api/contacts').json()['contacts']==[]
            rid=c.post('/api/rooms',json={'type':'dm','user_id':bid,'include_agent':False}).json()['room']['id']
            assert rid not in {r['id'] for r in b.get('/api/rooms').json()['rooms']}
            c.post(f'/api/rooms/{rid}/messages',json={'body':'HTTP 验收消息'}).raise_for_status()
            assert rid in {r['id'] for r in b.get('/api/rooms').json()['rooms']}
            b.post(f'/api/rooms/{rid}/messages',json={'body':'未添加联系人也能回复已有会话'}).raise_for_status()
            c.delete(f'/api/contacts/{bid}').raise_for_status()
            assert len(c.get(f'/api/rooms/{rid}/messages').json()['messages'])==2
            report={'url':url,'data_dir':str(data),'new_account_only_agent_room':True,'no_global_directory':True,'idempotent_one_way_add':True,'empty_dm_hidden_from_recipient':True,'recipient_replies_without_contact':True,'removal_preserves_history':True,'alice_id':aid,'bob_id':bid}
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            for name,login in [('login',alice),('bob-login',bob),('owner-login',owner)]:
                f=out/(name+'.json');f.write_text(json.dumps(login));f.chmod(0o600)
            print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
            if args.keep_running:
                try:
                    while True:time.sleep(1)
                except KeyboardInterrupt:pass
    finally:
        if server.poll() is None:
            os.killpg(server.pid,signal.SIGTERM)
            try:server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid,signal.SIGKILL);server.wait()


if __name__=='__main__':main()
