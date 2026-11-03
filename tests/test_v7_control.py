import pytest

from aichat import auth, chat, db, runtime, tasks


def test_old_attempt_and_revoked_credential_tokens_never_revive(env):
    env.say(env.a, env.agent_dm_a, "问题")
    old = env.lease()
    tid = old["task"]["id"]
    db.run("UPDATE agent_tasks SET lease_until=? WHERE id=?", (db.ago(1), tid))
    tasks.maintenance()
    current = env.lease()
    assert current["task"]["id"] == tid
    assert env.anon.get("/api/tools/whoami", headers=env.th(old)).status_code == 409
    assert env.anon.post(f"/api/agent/tasks/{tid}/renew", headers={**env.cred_h, "X-Task-Token": old["task_token"]}).status_code == 409
    auth.revoke_credential(env.cred_id)
    _, token = auth.issue_credential("replacement")
    newer = env.lease({"Authorization": "Bearer " + token})
    assert newer["task"]["id"] == tid
    assert env.anon.post("/api/tools/messages", headers=env.th(current), json={"body": "过时结果"}).status_code == 409
    assert env.anon.get("/api/tools/whoami", headers=env.th(newer)).status_code == 200


def test_control_endpoints_require_a_current_attempt_proof(env):
    env.say(env.a, env.agent_dm_a, "问题")
    lease = env.lease()
    h = {"Authorization": "Bearer " + env.cred}
    for action in ("renew", "complete", "fail"):
        r = env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/{action}", headers=h, json={})
        assert r.status_code in (401,409)


def test_reply_continues_agent_but_quoted_mentions_do_not_trigger(env):
    q = env.say(env.a, env.dm, "@agent 帮我分析")
    reply, _ = chat.send_message(env.dm, env.agent_id, "分析", reply_to=q["id"])
    follow = env.say(env.a, env.dm, "详细一点", reply_to=reply["id"])
    assert db.one("SELECT 1 FROM agent_tasks WHERE trigger_message_id=?", (follow["id"],))
    for text in ("```\n@agent\n```", "> @agent 是原文", "代码 `@agent`", "~~~text\n@agent\n~~~"):
        m = env.say(env.a, env.dm, text)
        assert not db.one("SELECT 1 FROM agent_tasks WHERE trigger_message_id=?", (m["id"],))


def test_only_initiator_or_owner_can_control_or_retry(env):
    env.say(env.b, env.dm, "@agent Bob 的任务")
    lease = env.lease()
    tid = lease["task"]["id"]
    # Carol is a member of the group but is neither initiator nor Owner.
    group = env.a.post("/api/rooms", json={"type":"group","title":"g","member_ids":[env.bob_id,env.carol_id]}).json()["room"]["id"]
    env.say(env.b, group, "@agent 第二个任务")
    second = env.lease()
    assert env.c.post(f"/api/tasks/{second['task']['id']}/cancel").status_code == 403
    assert env.b.post(f"/api/tasks/{second['task']['id']}/cancel").status_code == 200
    assert env.c.post(f"/api/tasks/{second['task']['id']}/retry").status_code == 403
    assert env.a.post(f"/api/tasks/{tid}/cancel").status_code == 200


def test_global_error_and_websocket_status_are_room_filtered(env):
    env.say(env.a, env.agent_dm_a, "私人任务")
    lease = env.lease()
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/fail", headers=env.cred_h, json={"error":"private-error","retry":False})
    assert env.a.get('/api/agent/status').json()['status']['last_error']
    assert env.b.get('/api/agent/status').json()['status']['last_error'] is None
    with env.b.websocket_connect('/ws') as ws:
        assert ws.receive_json()['agent']['last_error'] is None


def test_default_deployment_rejects_legacy_model_consumers(env):
    env.cfg.allow_legacy_agent = False
    env.say(env.a, env.agent_dm_a, "问题")
    assert env.anon.post('/api/agent/lease',headers=env.cred_h).status_code == 403


def test_text_attachment_is_readable_only_in_its_room(env):
    a = env.a.post(f'/api/rooms/{env.dm}/attachments',files={'file':('brief.md', '# 需求\n内容'.encode(),'text/markdown')}).json()['attachment']
    env.say(env.a,env.dm,'@agent 读文件',attachment_id=a['id'])
    lease = env.lease()
    r=env.anon.get(f"/api/tools/attachments/{a['id']}",headers=env.th(lease))
    assert r.status_code==200 and '# 需求' in r.text
    env.say(env.b,env.agent_dm_b,'另一个会话')
    other=env.lease()
    assert env.anon.get(f"/api/tools/attachments/{a['id']}",headers=env.th(other)).status_code==404
