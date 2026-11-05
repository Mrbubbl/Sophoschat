"""Contact behavior and upgrade boundaries, independent from old connected fixtures."""
import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from aichat import auth, backup, chat, config, contacts, db, hub
from aichat.contact_migration import apply, preview, KEY


def newcomer(env, name='dave'):
    c = TestClient(env.app)
    r = c.post('/api/register', json={'username': name, 'password': 'test-password-123'})
    assert r.status_code == 200, r.text
    return c, r.json()['user']['id']


def ids(c):
    return {u['id'] for u in c.get('/api/contacts').json()['contacts']}


def rooms(c):
    return c.get('/api/rooms').json()['rooms']


def test_new_registration_and_admin_creation_only_create_agent_room(env):
    before = {r['id'] for r in rooms(env.a)}
    c, uid = newcomer(env)
    assert ids(c) == set()
    assert [r['type'] for r in rooms(c)] == ['agent_dm']
    assert before == {r['id'] for r in rooms(env.a)}
    r = env.a.post('/api/users', json={'username':'erin','password':'password-erin'})
    uid2 = r.json()['user']['id']
    assert [r['type'] for r in chat.list_rooms(uid2)] == ['agent_dm']
    assert not contacts.list_for(uid2)
    chat.bootstrap_user(uid)
    assert len(rooms(c)) == 1


def test_exact_lookup_and_single_direction_idempotent_add_does_not_create_rooms(env):
    c, uid = newcomer(env)
    assert c.get('/api/users').json()['users'] == []
    r = c.get('/api/users/lookup', params={'username':'  ALICE  '})
    assert r.status_code == 200 and r.json()['user']['id'] == env.alice_id
    assert 'can_add_users' not in r.json()['user']
    counts = [db.one(f'SELECT COUNT(*) n FROM {t}')['n'] for t in ('rooms','messages','agent_tasks','events')]
    for _ in range(2):
        assert c.post('/api/contacts', json={'user_id':env.alice_id}).status_code == 200
    assert ids(c) == {env.alice_id}
    assert uid not in ids(env.a)
    assert counts == [db.one(f'SELECT COUNT(*) n FROM {t}')['n'] for t in ('rooms','messages','agent_tasks','events')]
    assert c.get('/api/users').json()['users'][0]['id'] == env.alice_id
    assert c.get('/api/users/lookup',params={'username':'Ali'}).status_code == 404
    assert c.get('/api/users/lookup',params={'username':'dave'}).status_code == 400
    assert c.post('/api/contacts',json={'user_id':uid}).status_code == 400
    assert c.post('/api/contacts',json={'user_id':env.agent_id}).status_code == 404


def test_first_dm_only_visible_to_opener_then_recipient_can_reply_without_contact(env):
    c, uid = newcomer(env)
    assert c.post('/api/rooms',json={'type':'dm','user_id':env.alice_id}).status_code == 403
    c.post('/api/contacts',json={'user_id':env.alice_id})
    rid = c.post('/api/rooms',json={'type':'dm','user_id':env.alice_id,'include_agent':False}).json()['room']['id']
    assert rid in {r['id'] for r in rooms(c)}
    assert rid not in {r['id'] for r in rooms(env.a)}
    env.say(c,rid,'hello Alice')
    assert rid in {r['id'] for r in rooms(env.a)}
    assert uid not in ids(env.a)
    env.say(env.a,rid,'hello Dave')
    for _ in range(2):
        assert c.delete(f'/api/contacts/{env.alice_id}').status_code == 200
    env.say(c,rid,'still able to talk in existing room')
    assert len(c.get(f'/api/rooms/{rid}/messages').json()['messages']) == 3
    c.post('/api/contacts',json={'user_id':env.alice_id})
    assert c.post('/api/rooms',json={'type':'dm','user_id':env.alice_id}).json()['room']['id'] == rid


def test_only_contact_owner_and_managers_access_their_respective_data(env):
    c, uid = newcomer(env)
    assert c.get('/api/admin/users').status_code == 403
    assert env.anon.get('/api/contacts').status_code == 401
    assert env.anon.get('/api/users/lookup?username=alice').status_code == 401
    assert c.post('/api/contacts',json={'user_id':env.alice_id,'owner_user_id':env.bob_id}).status_code == 422
    assert ids(c) == set()
    assert env.bob_id in ids(env.a)
    c.delete(f'/api/contacts/{env.bob_id}')
    assert env.bob_id in ids(env.a)
    assert c.get('/api/contacts',params={'owner_id':env.alice_id}).json()['contacts'] == []
    assert uid in {u['id'] for u in env.a.get('/api/admin/users').json()['users']}
    env.a.patch(f'/api/users/{uid}/permissions',json={'can_add_users':True})
    assert c.get('/api/admin/users').status_code == 200
    assert c.get('/api/users').json()['users'] == []


def test_lookup_rate_window_survives_new_client_and_resets_after_time(env,monkeypatch):
    c, uid = newcomer(env)
    import time
    now = time.time()
    for _ in range(20):
        assert c.get('/api/users/lookup?username=alice').status_code == 200
    other = TestClient(env.app)
    other.post('/api/login',json={'username':'dave','password':'test-password-123'})
    assert other.get('/api/users/lookup?username=alice').status_code == 429
    assert len(db.jd(db.get_kv(f'contacts_lookup:{uid}'))) == 20
    monkeypatch.setattr(time,'time',lambda:now+62)
    assert c.get('/api/users/lookup?username=alice').status_code == 200
    assert c.get('/api/contacts').status_code == 200


def test_disabled_contact_keeps_history_and_can_be_removed(env):
    c, uid = newcomer(env)
    c.post('/api/contacts',json={'user_id':env.bob_id})
    rid = c.post('/api/rooms',json={'type':'dm','user_id':env.bob_id}).json()['room']['id']
    env.say(c,rid,'past conversation')
    auth.set_disabled(env.bob_id,True)
    assert c.get('/api/contacts').json()['contacts'][0]['disabled']
    assert c.get('/api/users/lookup?username=bob').status_code == 404
    assert c.post('/api/contacts',json={'user_id':env.bob_id}).status_code == 404
    assert c.post('/api/rooms',json={'type':'dm','user_id':env.bob_id}).status_code == 404
    assert c.get(f'/api/rooms/{rid}/messages').status_code == 200
    assert c.delete(f'/api/contacts/{env.bob_id}').status_code == 200


def test_group_selection_is_scoped_but_membership_and_invite_dont_add_contacts(env):
    c, uid = newcomer(env)
    assert c.post('/api/rooms',json={'type':'group','title':'group','member_ids':[env.bob_id]}).status_code == 403
    env.a.post('/api/contacts',json={'user_id':uid})
    rid = env.a.post('/api/rooms',json={'type':'group','title':'group','member_ids':[uid]}).json()['room']['id']
    assert ids(c) == set()
    assert c.post(f'/api/rooms/{rid}/members',json={'user_id':env.bob_id}).status_code == 403
    c.post('/api/contacts',json={'user_id':env.bob_id})
    assert c.post(f'/api/rooms/{rid}/members',json={'user_id':env.bob_id}).status_code == 200
    assert uid not in ids(env.b)
    invitation = env.a.post('/api/invites',json={'hours':24,'max_uses':1,'room_id':rid}).json()
    fresh = TestClient(env.app)
    r = fresh.post('/api/register',json={'username':'invitee','password':'invitee-password','invite':invitation['token']})
    assert r.status_code == 200
    assert ids(fresh) == set()
    assert {r['type'] for r in rooms(fresh)} == {'group','agent_dm'}
    assert rid in {r['id'] for r in rooms(fresh)}


def test_same_account_devices_receive_contact_changes_only(env):
    c, uid = newcomer(env)
    device = TestClient(env.app)
    device.post('/api/login',json={'username':'dave','password':'test-password-123'})
    with c.websocket_connect('/ws') as ws1, device.websocket_connect('/ws') as ws2:
        assert ws1.receive_json()['type'] == ws2.receive_json()['type'] == 'hello'
        assert c.post('/api/contacts',json={'user_id':env.alice_id}).status_code == 200
        assert ws1.receive_json()['type'] == ws2.receive_json()['type'] == 'contacts.updated'
        assert ids(device) == {env.alice_id}
        c.delete(f'/api/contacts/{env.alice_id}')
        assert ws1.receive_json()['type'] == ws2.receive_json()['type'] == 'contacts.updated'
        assert ids(device) == set()


def test_profile_updates_only_reach_known_audience(env,monkeypatch):
    c, uid = newcomer(env)
    c.post('/api/contacts',json={'user_id':env.bob_id})
    outsider, oid = newcomer(env,'outsider')
    seen=[]
    monkeypatch.setattr(hub,'publish',lambda recipients,payload:seen.append((set(recipients),payload)))
    from aichat.api import _avatar_changed
    _avatar_changed(env.bob_id)
    assert uid in seen[-1][0] and oid not in seen[-1][0]
    assert seen[-1][1]['type']=='user.updated'


def test_legacy_migration_preview_is_read_only_and_apply_is_idempotent(env,tmp_path):
    # Simulate a version-9 snapshot without importing old DM pairs as contacts.
    env.a.post('/api/rooms',json={'type':'dm','user_id':env.carol_id})
    kept=env.dm
    msg=env.say(env.a,kept,'deleted message still counts as history')
    env.a.delete(f"/api/messages/{msg['id']}")
    empty=db.one("SELECT id FROM rooms WHERE dm_key=?",(f'dm:{env.alice_id}:{env.carol_id}',))['id']
    db.run('DELETE FROM kv WHERE key=?',(KEY,))
    db.run('PRAGMA user_version=9')
    plan=preview(db.conn())
    assert plan['rooms']==1 and plan['room_ids']==[empty]
    db.run('DELETE FROM contacts')
    db.close()
    path=env.cfg.db_path
    before=path.read_bytes()
    out=subprocess.run([sys.executable,'-m','aichat.cli','--data-dir',str(env.cfg.data_dir),'contacts-migration','--preview'],capture_output=True,text=True,check=True)
    assert json.loads(out.stdout)['rooms']==1
    assert path.read_bytes()==before
    db.connect(path)
    assert db.one('PRAGMA user_version')[0]==10
    assert db.one('SELECT archived FROM room_members WHERE room_id=? AND user_id=?',(empty,env.alice_id))['archived']==1
    assert db.one('SELECT archived FROM room_members WHERE room_id=? AND user_id=?',(kept,env.alice_id))['archived']==0
    assert contacts.list_for(env.alice_id)==[]
    db.run('UPDATE room_members SET archived=0 WHERE room_id=?',(empty,))
    assert apply(db.conn())['applied']
    assert db.one('SELECT archived FROM room_members WHERE room_id=?',(empty,))['archived']==0


def test_contacts_survive_backup_without_leaking_to_room_export(env,tmp_path):
    c,uid=newcomer(env)
    c.post('/api/contacts',json={'user_id':env.alice_id})
    env.say(c,chat.ensure_agent_dm(uid),'private note')
    exported=c.get(f'/api/rooms/{chat.ensure_agent_dm(uid)}/export?format=json').text
    assert 'contact_user_id' not in exported
    archive=tmp_path/'contacts-backup.tar.gz'
    backup.create_backup(archive)
    assert not [x for x in backup.restore_backup(archive,tmp_path/'restored') if x['level']=='error']
    assert {u['id'] for u in contacts.list_for(uid)} == {env.alice_id}


def test_cli_creates_only_agent_room(env):
    out=subprocess.run([sys.executable,'-m','aichat.cli','--data-dir',str(env.cfg.data_dir),'user','add','cli_user','--password','test-cli-password'],capture_output=True,text=True,check=True)
    assert '联系人需手动添加' in out.stdout
    uid=auth.get_user(db.one('SELECT id FROM users WHERE username=?',('cli_user',))['id'])['id']
    assert [r['type'] for r in chat.list_rooms(uid)]==['agent_dm']
    assert contacts.list_for(uid)==[]


def test_migration_preserves_empty_rooms_with_tasks_or_artifacts_and_groups(env):
    task_room = env.dm
    artifact_room = chat.create_dm(env.alice_id, env.carol_id)
    empty_room = chat.create_dm(env.bob_id, env.carol_id)
    group = chat.create_group(env.alice_id,'keep group',[env.bob_id])
    db.run("INSERT INTO agent_tasks(room_id,kind,status,created_at,expires_at,params) VALUES(?,'chat','failed',?,?,'{}')",(task_room,db.now(),db.after(600)))
    db.run("INSERT INTO artifacts(room_id,kind,title,initiator_id,created_at) VALUES(?,'markdown','keep file',?,?)",(artifact_room,env.alice_id,db.now()))
    db.run('DELETE FROM kv WHERE key=?',(KEY,))
    plan=preview(db.conn())
    assert plan['room_ids']==[empty_room] and plan['memberships']==2
    with db.tx(): apply(db.conn())
    for rid in (task_room,artifact_room,group,env.agent_dm_a):
        assert db.one("SELECT COUNT(*) n FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND u.role!='agent' AND m.archived!=0",(rid,))['n']==0
    assert db.one("SELECT COUNT(*) n FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND u.role!='agent' AND m.archived=1",(empty_room,))['n']==2
    assert db.one('SELECT COUNT(*) n FROM artifacts')['n']==1
    assert db.one('SELECT COUNT(*) n FROM agent_tasks')['n']==1
