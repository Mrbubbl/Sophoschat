import json

import pytest

from aichat import artifacts, auth, backup, chat, config, db, runtime, tasks


class SDKEnv:
    def __init__(self, e):
        self.e=e
        self.token=runtime.service_credential()
        self.headers={"Authorization":"Bearer "+self.token}
        runtime.update_settings({"api_key":"test-only-not-a-real-key","model":"claude-test","enabled":True},e.alice_id)

    def lease(self, profiles="chat,delivery,memory"):
        r=self.e.anon.post('/api/agent/lease',headers=self.headers,params={'engine':'claude-sdk','owner':'test-sdk','profiles':profiles})
        assert r.status_code==200,r.text
        return r.json()['lease']

    def control(self, lease):
        return {**self.headers,'X-Task-Token':lease['task_token']}

    def result(self, lease, cost=0.01, outcome='completed'):
        return self.e.anon.post(f"/api/agent/tasks/{lease['task']['id']}/result",headers=self.control(lease),json={'sdk_version':runtime.SDK_VERSION,'cost_usd':cost,'usage':{'input_tokens':20,'output_tokens':10},'session_id':'session-test','outcome':outcome})

    def complete(self, lease):
        assert self.result(lease).status_code==200
        if lease['task']['profile']=='chat':
            r=self.e.anon.post('/api/tools/messages',headers=self.e.th(lease),json={'body':'测试回复','idempotency_key':f"task-{lease['task']['id']}"})
            assert r.status_code==200,r.text
        return self.e.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete",headers=self.control(lease),json={})

    def delivery(self, body='@agent 写一份方案.md'):
        self.e.say(self.e.a,self.e.dm,body)
        parent=self.lease('chat')
        r=self.e.anon.post('/api/tools/deliveries',headers=self.e.th(parent),json={'instruction':body})
        assert r.status_code==200,r.text
        assert '交付文件卡片' in self.result(parent).json()['reply_override']
        assert self.complete(parent).status_code==200
        child=self.lease('delivery')
        assert child
        return child

    def publish(self, lease, text='# 活动方案\n\n第一版', filename='方案.md'):
        return self.e.anon.post('/api/tools/artifacts/publish',headers=self.e.th(lease),json={'filename':filename,'content':text})


@pytest.fixture
def sdk(env):
    return SDKEnv(env)


def test_owner_config_is_encrypted_and_not_exposed_to_members(env,sdk):
    r=env.a.get('/api/agent/settings')
    assert r.status_code==200 and r.json()['settings']['configured']
    assert 'test-only-not-a-real-key' not in r.text
    assert 'test-only-not-a-real-key' not in db.get_kv('server_agent_settings')
    assert env.b.get('/api/agent/settings').status_code==403
    assert env.anon.get('/api/agent/runtime',headers=env.cred_h).status_code==403
    assert env.b.put('/api/agent/settings',json={'enabled':False}).status_code==403


def test_sessions_are_scoped_serial_and_configuration_revocation_fences_workers(env,sdk):
    env.say(env.a,env.agent_dm_a,'第一条')
    one=sdk.lease('chat')
    env.say(env.a,env.agent_dm_a,'第二条')
    assert sdk.lease('chat') is None
    env.say(env.b,env.agent_dm_b,'另一个会话')
    other=sdk.lease('chat')
    assert other and other['task']['room_id']!=one['task']['room_id']
    one_state=env.anon.get(f"/api/agent/tasks/{one['task']['id']}/runtime",headers=sdk.control(one)).json()
    other_state=env.anon.get(f"/api/agent/tasks/{other['task']['id']}/runtime",headers=sdk.control(other)).json()
    assert one_state['session']['session_key']!=other_state['session']['session_key']
    runtime.update_settings({'enabled':False},env.alice_id)
    assert env.anon.get('/api/tools/whoami',headers=env.th(one)).status_code==409
    assert sdk.lease() is None


def test_long_delivery_keeps_interactive_capacity_and_memory_cannot_upgrade(env,sdk):
    delivery=sdk.delivery()
    env.say(env.b,env.agent_dm_b,'还能回答吗')
    interactive=sdk.lease('chat')
    assert interactive and delivery
    assert sdk.complete(interactive).status_code==200
    assert sdk.complete(delivery).status_code==409  # no actual file
    env.cfg.consolidate_quiet=0
    tasks.maintenance()
    # Free the background slot then lease memory.
    env.a.post(f"/api/tasks/{delivery['task']['id']}/cancel")
    memory=sdk.lease('memory')
    assert memory and memory['task']['profile']=='memory'
    assert env.anon.post('/api/tools/deliveries',headers=env.th(memory),json={'instruction':'publish'}).status_code==403


def test_budget_reservations_count_unknown_cost_and_retries(env,sdk):
    runtime.update_settings({'monthly_usd':0.25},env.alice_id)
    env.say(env.a,env.agent_dm_a,'第一次')
    first=sdk.lease('chat')
    env.say(env.b,env.agent_dm_b,'第二次')
    assert sdk.lease('chat') is None
    second=db.one("SELECT * FROM agent_tasks WHERE trigger_message_id=(SELECT MAX(id) FROM messages)")
    assert second['status']=='failed' and second['error']=='agent_budget_exhausted'
    usage=env.a.get('/api/agent/usage').json()
    assert usage['unknown_attempts']==1 and usage['attempts'][0]['cost_usd'] is None
    assert sdk.result(first,0.03).status_code==200
    assert sdk.result(first,0.04).status_code==200
    assert env.a.get('/api/agent/usage').json()['known_cost_usd']==0.03


def test_markdown_delivery_revision_is_immutable_and_survives_restore(env,sdk,tmp_path):
    first=sdk.delivery()
    v1=sdk.publish(first).json()['artifact_version']
    assert v1['number']==1 and sdk.complete(first).status_code==200
    r=env.a.get(v1['download_url'])
    assert r.status_code==200 and r.text=='# 活动方案\n\n第一版'
    assert 'text/markdown' in r.headers['content-type']
    again=sdk.publish(first)  # completed token cannot publish again
    assert again.status_code==409
    env.say(env.a,env.dm,'精简到一行',reply_to=v1['message_id'])
    second=sdk.lease('delivery')
    assert second['task']['base_version_id']==v1['id']
    v2=sdk.publish(second,'# 精简方案').json()['artifact_version']
    assert v2['number']==2 and v2['artifact_id']==v1['artifact_id'] and v2['parent_version_id']==v1['id']
    assert sdk.complete(second).status_code==200
    assert env.a.get(v1['download_url']).text=='# 活动方案\n\n第一版'
    assert env.b.get(v2['download_url']).text=='# 精简方案'
    assert env.c.get(v2['download_url']).status_code in (403,404)
    assert env.anon.get(v2['download_url']).status_code==401
    archive=tmp_path/'backup.tar.gz';backup.create_backup(archive)
    backup.restore_backup(archive,tmp_path/'restored')
    assert artifacts.file_path(artifacts.version_row(v1['id'])).read_text()=='# 活动方案\n\n第一版'
    assert artifacts.file_path(artifacts.version_row(v2['id'])).read_text()=='# 精简方案'
    assert runtime.settings()['configured'] and runtime.settings(secrets=True)['api_key']=='test-only-not-a-real-key'


def test_publish_is_idempotent_and_conflicting_parent_does_not_overwrite(env,sdk):
    delivery=sdk.delivery()
    one=sdk.publish(delivery).json()['artifact_version']
    assert sdk.publish(delivery).json()['artifact_version']['id']==one['id']
    assert sdk.publish(delivery,'different').status_code==409
    assert db.one('SELECT COUNT(*) n FROM artifact_versions')['n']==1
    assert db.one('SELECT COUNT(*) n FROM messages WHERE artifact_version_id IS NOT NULL')['n']==1
    assert env.anon.post('/api/tools/artifacts/publish',headers=env.th(delivery),json={'filename':'../x.md','content':'x'}).status_code==422


def test_html_download_and_private_preview_have_distinct_security_contracts(env,sdk):
    delivery=sdk.delivery('@agent 做一个网页')
    html='<!doctype html><html><head><title>测试</title></head><body><button onclick="this.textContent=1">点我</button></body></html>'
    v=sdk.publish(delivery,html,'页面.html').json()['artifact_version']
    r=env.a.get(v['preview_url'])
    assert r.status_code==200
    csp=r.headers['content-security-policy']
    assert 'sandbox allow-scripts' in csp and 'allow-same-origin' not in csp and "connect-src 'none'" in csp
    assert r.headers['x-frame-options']=='SAMEORIGIN'
    assert 'attachment' in env.a.get(v['download_url']).headers['content-disposition']
    assert env.anon.get(v['preview_url']).status_code==401
    assert env.c.get(v['preview_url']).status_code in (403,404)


def test_selfcheck_does_not_pass_until_sdk_read_publish_and_readback(env,sdk,monkeypatch):
    monkeypatch.setattr(runtime,'installed_sdk',lambda:{'installed':True,'version':runtime.SDK_VERSION,'expected':runtime.SDK_VERSION})
    env.anon.post('/api/agent/heartbeat',headers=sdk.headers,json={'adapter':'claude-sdk'})
    r=env.a.post('/api/agent/selfcheck').json()
    assert not r['ok'] and r['state']=='pending'
    lease=sdk.lease('delivery');assert lease['task']['id']==r['task_id']
    env.anon.get('/api/tools/whoami',headers=env.th(lease))
    v=sdk.publish(lease,'# SDK 自检','selfcheck.md').json()['artifact_version']
    assert sdk.complete(lease).status_code==409
    env.anon.get(f"/api/tools/artifacts/{v['id']}",headers=env.th(lease))
    assert sdk.complete(lease).status_code==200
    final=env.a.get(f"/api/agent/selfcheck/{r['task_id']}").json()
    assert final['ok'] and all(c['ok'] for c in final['checks'])
    env.cfg.consolidate_quiet=0;tasks.maintenance()
    memory=sdk.lease('memory')
    if memory:
        batch=env.anon.get('/api/tools/batch',headers=env.th(memory)).json()
        assert batch['messages']==[]


def test_conflicting_revision_cannot_overwrite_newer_version(env,sdk):
    first=sdk.delivery()
    v1=sdk.publish(first).json()['artifact_version']
    assert sdk.complete(first).status_code==200
    env.say(env.a,env.dm,'改稿 A',reply_to=v1['message_id'])
    env.say(env.a,env.dm,'改稿 B',reply_to=v1['message_id'])
    a=sdk.lease('delivery')
    assert sdk.publish(a,'# A').status_code==200
    assert sdk.complete(a).status_code==200
    b=sdk.lease('delivery')
    r=sdk.publish(b,'# B')
    assert r.status_code==409 and r.json()['error']['code']=='artifact_version_conflict'
    assert db.one('SELECT COUNT(*) n FROM artifact_versions')['n']==2


def test_cancelled_published_delivery_retries_same_logical_file(env,sdk):
    delivery=sdk.delivery()
    version=sdk.publish(delivery).json()['artifact_version']
    tid=delivery['task']['id']
    assert env.a.post(f'/api/tasks/{tid}/cancel').status_code==200
    retried=env.a.post(f'/api/tasks/{tid}/retry').json()['task']
    assert retried['id']==tid
    new=sdk.lease('delivery')
    assert env.anon.get('/api/tools/artifacts',headers=env.th(new)).json()['versions'][0]['id']==version['id']
    assert sdk.publish(new).json()['artifact_version']['id']==version['id']
    assert sdk.complete(new).status_code==200
    assert db.one('SELECT COUNT(*) n FROM artifact_versions')['n']==1


def test_monthly_usage_total_is_not_truncated_by_detail_limit(env,sdk):
    for i in range(205):
        db.run("INSERT INTO task_attempts(task_id,attempt,generation,room_id,profile,reserved_usd,cost_usd,started_at) VALUES(?,1,?,?,'chat',1,1,?)",(1000+i,f'usage-{i}',env.dm,db.now()))
    u=env.a.get('/api/agent/usage').json()
    assert len(u['attempts'])==200 and u['known_cost_usd']==205


def test_session_reset_is_scoped_to_platform_current_attempt(env,sdk):
    env.say(env.a,env.agent_dm_a,'会话恢复')
    lease=sdk.lease('chat');tid=lease['task']['id']
    state=env.anon.get(f'/api/agent/tasks/{tid}/runtime',headers=sdk.control(lease)).json()
    db.run('UPDATE sdk_sessions SET sdk_session_id=?,total_cost=1 WHERE session_key=?',('old-session',state['session']['session_key']))
    assert env.a.post(f'/api/agent/tasks/{tid}/session-reset').status_code==401
    assert env.anon.post(f'/api/agent/tasks/{tid}/session-reset',headers=sdk.control(lease)).status_code==200
    assert db.one('SELECT sdk_session_id FROM sdk_sessions WHERE session_key=?',(state['session']['session_key'],))['sdk_session_id'] is None


# ---------------------------------------------------------------- v22：双接入（日常 + 高级）
def _attempt_cfg(env, sdk, lease):
    r = env.anon.get(f"/api/agent/tasks/{lease['task']['id']}/runtime", headers=sdk.control(lease))
    assert r.status_code == 200, r.text
    return r.json()['config']


def test_premium_settings_validation_and_secret_never_exposed(env, sdk):
    with pytest.raises(Exception):
        runtime.update_settings({'premium_model': 'big-model'}, env.alice_id)  # 只填模型不填 Key
    runtime.update_settings({'premium_model': 'big-model', 'premium_base_url': 'https://pro.example.com/anthropic', 'premium_api_key': 'premium-secret-key'}, env.alice_id)
    r = env.a.get('/api/agent/settings')
    assert r.status_code == 200 and r.json()['settings']['premium_configured']
    assert 'premium-secret-key' not in r.text and 'premium-secret-key' not in db.get_kv('server_agent_settings')
    with pytest.raises(Exception):
        runtime.update_settings({'premium_base_url': 'http://pro.example.com'}, env.alice_id)  # 非 HTTPS
    # 清空高级模型和 Key 后回到只有日常接入
    runtime.update_settings({'premium_model': '', 'premium_api_key': ''}, env.alice_id)
    assert not env.a.get('/api/agent/settings').json()['settings']['premium_configured']


def test_routing_chat_uses_main_pro_prefix_and_delivery_use_premium(env, sdk):
    runtime.update_settings({'base_url': 'https://cheap.example.com/anthropic', 'model': 'cheap-model'}, env.alice_id)
    # 没配高级接入：/pro 自动忽略，仍走日常
    env.say(env.a, env.agent_dm_a, '/pro 你好')
    lease = sdk.lease('chat')
    cfg = _attempt_cfg(env, sdk, lease)
    assert cfg['model'] == 'cheap-model' and cfg['api_key'] == 'test-only-not-a-real-key'
    assert sdk.complete(lease).status_code == 200

    runtime.update_settings({'premium_model': 'big-model', 'premium_base_url': 'https://pro.example.com/anthropic', 'premium_api_key': 'premium-secret-key'}, env.alice_id)
    env.say(env.a, env.agent_dm_a, '普通问题')
    lease = sdk.lease('chat')
    cfg = _attempt_cfg(env, sdk, lease)
    assert (cfg['model'], cfg['base_url'], cfg['api_key']) == ('cheap-model', 'https://cheap.example.com/anthropic', 'test-only-not-a-real-key')
    assert sdk.complete(lease).status_code == 200

    env.say(env.a, env.agent_dm_a, '/pro 这个要认真答')
    lease = sdk.lease('chat')
    cfg = _attempt_cfg(env, sdk, lease)
    assert (cfg['model'], cfg['base_url'], cfg['api_key']) == ('big-model', 'https://pro.example.com/anthropic', 'premium-secret-key')
    assert sdk.complete(lease).status_code == 200

    child = sdk.delivery()
    cfg = _attempt_cfg(env, sdk, child)
    assert cfg['model'] == 'big-model' and cfg['api_key'] == 'premium-secret-key'


def test_consolidation_waits_in_queue_when_budget_exhausted(env, sdk):
    runtime.update_settings({'monthly_usd': 0.25, 'room_monthly_usd': 0.25, 'user_monthly_usd': 0.25}, env.alice_id)
    env.say(env.a, env.agent_dm_a, '占满额度')
    first = sdk.lease('chat')
    assert first and sdk.result(first, 0.25).status_code == 200
    assert sdk.complete(first).status_code in (200, 409)
    room_id = env.agent_dm_a
    db.run("INSERT INTO agent_tasks(room_id, kind, profile, status, created_at, params) VALUES(?, 'consolidate', 'memory', 'queued', ?, ?)", (room_id, db.now(), db.jl({'mode': 'pending', 'event_from': 0, 'event_to': 1})))
    for _ in range(3):
        assert sdk.lease('memory') is None
    row = db.one("SELECT status, error FROM agent_tasks WHERE kind='consolidate' ORDER BY id DESC LIMIT 1")
    assert row['status'] == 'queued' and not row['error']
