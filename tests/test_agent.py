from aichat import auth, chat, config, db, tasks


def n_tasks(kind="chat", status=None):
    sql, args = "SELECT COUNT(*) AS c FROM agent_tasks WHERE kind=?", [kind]
    if status:
        sql += " AND status=?"
        args.append(status)
    return db.one(sql, args)["c"]


def test_chat_trigger_rules(env):
    gid = env.a.post("/api/rooms", json={"type": "group", "title": "g", "member_ids": [env.bob_id]}).json()["room"]["id"]
    env.say(env.a, gid, "大家好")
    assert n_tasks() == 0  # 群聊普通消息不触发
    env.say(env.a, gid, "@agent 你怎么看")
    assert n_tasks() == 1
    env.say(env.a, env.dm, "只是聊天，没有 @")
    assert n_tasks() == 1  # 带 Agent 的双人私聊同样默认不打扰
    env.say(env.a, env.agent_dm_a, "直接问你")
    assert n_tasks() == 2  # Agent 私聊每条都触发
    # Agent 自己的消息永远不触发任务，即使提到了自己
    chat.send_message(gid, env.agent_id, "@agent 我在自言自语", "loop1")
    assert n_tasks() == 2


def test_agent_not_in_room_means_no_task(env):
    gid = env.a.post("/api/rooms", json={"type": "group", "title": "g", "member_ids": [env.bob_id], "include_agent": False}).json()["room"]["id"]
    env.say(env.a, gid, "@agent 在吗")
    assert n_tasks() == 0


def test_lease_reply_complete_flow_and_idempotency(env):
    q = env.say(env.a, env.agent_dm_a, "今天天气怎么样？")
    lease = env.lease()
    assert lease["purpose"] == "chat" and lease["task"]["status"] == "leased"
    th = env.th(lease)
    who = env.anon.get("/api/tools/whoami", headers=th).json()
    assert who["trigger_message"]["id"] == q["id"] and who["room"]["type"] == "agent_dm"
    r1 = env.anon.post("/api/tools/messages", headers=th, json={"body": "我没有实时天气数据。"})
    r2 = env.anon.post("/api/tools/messages", headers=th, json={"body": "我没有实时天气数据。"})
    assert r1.status_code == 200 and r2.json()["duplicate"] is True
    assert r1.json()["message"]["id"] == r2.json()["message"]["id"]
    assert r1.json()["message"]["kind"] == "agent" and r1.json()["message"]["reply_to"] == q["id"]
    done = env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={"result_message_id": r1.json()["message"]["id"]})
    assert done.json()["task"]["status"] == "done"
    # 完成后令牌立即失效
    late = env.anon.post("/api/tools/messages", headers=th, json={"body": "迟到的回复"})
    assert late.status_code == 409 and late.json()["error"]["code"] == "lease_not_held"
    assert [m["body"] for m in env.a.get(f"/api/rooms/{env.agent_dm_a}/messages").json()["messages"]].count("我没有实时天气数据。") == 1
    # Agent 的回复不会再触发新任务
    assert n_tasks("chat", "queued") == 0


def test_task_token_is_scoped_to_one_room(env):
    secret_dm = env.a.post("/api/rooms", json={"type": "dm", "user_id": env.carol_id}).json()["room"]["id"]
    env.say(env.a, secret_dm, "只属于 alice 和 carol 的秘密")
    env.say(env.b, env.agent_dm_b, "bob 在问 agent")
    lease = env.lease()
    th = env.th(lease)
    assert lease["task"]["room_id"] == env.agent_dm_b
    # 无论怎么传参数，读到的永远只有任务所在 Room 的内容
    for params in ({}, {"room_id": secret_dm}, {"around_seq": 1}, {"q": "秘密"}):
        msgs = env.anon.get("/api/tools/messages", headers=th, params=params).json()["messages"]
        assert all(m["room_id"] == env.agent_dm_b for m in msgs)
    # 不能用别的 Room 的消息 ID 取回复链
    other = db.one("SELECT id FROM messages WHERE room_id=?", (secret_dm,))["id"]
    assert env.anon.get(f"/api/tools/messages/{other}/thread", headers=th).status_code == 404
    # 伪造/篡改的令牌无效
    bad = {"Authorization": "Bearer " + lease["task_token"][:-3] + "AAA"}
    assert env.anon.get("/api/tools/whoami", headers=bad).status_code == 401
    # 普通用户会话不能当成任务令牌
    assert env.a.get("/api/tools/whoami").status_code == 401


def test_wrong_purpose_and_credentials(env):
    env.say(env.a, env.agent_dm_a, "问题")
    chat_lease = env.lease()
    assert env.anon.get("/api/tools/batch", headers=env.th(chat_lease)).status_code == 403
    assert env.anon.post("/api/tools/memory/commit", headers=env.th(chat_lease), json={"ops": []}).status_code == 403
    # 应用凭证不能直接调用工具；任务令牌不能做控制层操作
    assert env.anon.get("/api/tools/whoami", headers=env.cred_h).status_code == 401
    assert env.anon.post("/api/agent/heartbeat", headers=env.th(chat_lease), json={}).status_code == 401
    assert env.anon.post("/api/agent/lease", headers={"Authorization": "Bearer aca_nope"}).status_code == 401
    # 另一个 Agent 凭证不能完成别人领取的任务
    _, other = auth.issue_credential("other")
    r = env.anon.post(f"/api/agent/tasks/{chat_lease['task']['id']}/complete", headers={"Authorization": f"Bearer {other}"}, json={})
    assert r.status_code == 409


def test_revoking_credential_takes_effect_immediately(env):
    env.say(env.a, env.agent_dm_a, "问题")
    lease = env.lease()
    th = env.th(lease)
    assert env.anon.get("/api/tools/whoami", headers=th).status_code == 200
    assert env.a.delete(f"/api/agent/credentials/{env.cred_id}").status_code == 200
    r = env.anon.get("/api/tools/whoami", headers=th)
    assert r.status_code in (401, 409)
    assert env.anon.post("/api/agent/lease", headers=env.cred_h).status_code == 401
    # 只有 Owner 能管理凭证
    assert env.b.post("/api/agent/credentials", json={"label": "x"}).status_code == 403


def test_chat_task_expires_and_can_be_retried(env):
    env.cfg.chat_task_ttl = -1
    q = env.say(env.a, env.agent_dm_a, "很久没人处理的问题")
    stats = tasks.maintenance()
    assert stats["expired"] == 1
    assert env.lease() is None  # 不会补发过时回答
    t = env.a.get(f"/api/rooms/{env.agent_dm_a}/tasks").json()["tasks"][0]
    assert t["status"] == "expired" and t["trigger_message_id"] == q["id"]
    env.cfg.chat_task_ttl = 600
    new = env.a.post(f"/api/tasks/{t['id']}/retry").json()["task"]
    assert new["status"] == "queued"
    assert env.a.post(f"/api/tasks/{new['id']}/retry").status_code == 409  # 仍在排队，不可重复重试
    assert env.lease()["task"]["id"] == new["id"]


def test_lease_loss_requeues_then_fails_after_max_attempts(env):
    env.say(env.a, env.agent_dm_a, "会被反复领取的任务")
    env.cfg.lease_seconds = -5  # 领取后租约立即过期，模拟 Bridge 崩溃
    for attempt in (1, 2):
        lease = env.lease()
        assert lease["task"]["attempts"] == attempt
        stale = env.anon.get("/api/tools/whoami", headers=env.th(lease))
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "lease_not_held"
        assert tasks.maintenance()["requeued"] == 1
    lease = env.lease()
    assert lease["task"]["attempts"] == 3
    assert tasks.maintenance()["failed"] == 1
    assert env.lease() is None


def test_fail_endpoint_retries_then_gives_up(env):
    env.say(env.a, env.agent_dm_a, "q")
    lease = env.lease()
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/fail", headers=env.cred_h, json={"error": "模型超时", "retry": True})
    assert db.one("SELECT status FROM agent_tasks WHERE id=?", (lease["task"]["id"],))["status"] == "queued"
    lease = env.lease()
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/fail", headers=env.cred_h, json={"error": "模型超时", "retry": False})
    assert db.one("SELECT status FROM agent_tasks WHERE id=?", (lease["task"]["id"],))["status"] == "failed"
    st = env.a.get("/api/agent/status").json()["status"]
    assert "模型超时" in st["last_error"]["error"]


def test_deleting_trigger_message_cancels_task_and_blocks_reply(env):
    q = env.say(env.a, env.agent_dm_a, "我后悔了")
    lease = env.lease()
    env.a.delete(f"/api/messages/{q['id']}")
    r = env.anon.post("/api/tools/messages", headers=env.th(lease), json={"body": "回复一条已经删除的消息"})
    assert r.status_code == 409
    assert db.one("SELECT status FROM agent_tasks WHERE id=?", (lease["task"]["id"],))["status"] == "cancelled"


def test_removing_agent_from_room_stops_everything(env):
    gid = env.a.post("/api/rooms", json={"type": "group", "title": "g", "member_ids": [env.bob_id]}).json()["room"]["id"]
    env.say(env.a, gid, "@agent 在吗")
    lease = env.lease()
    env.a.post(f"/api/rooms/{gid}/agent", json={"enabled": False})
    assert env.anon.get("/api/tools/whoami", headers=env.th(lease)).status_code == 409
    env.say(env.a, gid, "@agent 还在吗")
    assert env.lease() is None
    # 重新邀请后默认只能看到加入之后的内容
    env.a.post(f"/api/rooms/{gid}/agent", json={"enabled": True, "history": "from_now"})
    env.say(env.a, gid, "@agent 回来了")
    lease = env.lease()
    msgs = env.anon.get("/api/tools/messages", headers=env.th(lease)).json()["messages"]
    assert [m["body"] for m in msgs] == ["@agent 回来了"]


def test_consolidation_scheduling_and_cursor(env):
    env.say(env.a, env.dm, "第一条")
    env.say(env.b, env.dm, "第二条")
    assert tasks.maintenance()["consolidations"] == 0  # 讨论刚发生，仍在等待静默
    env.cfg.consolidate_quiet = 0
    assert tasks.maintenance()["consolidations"] == 1
    assert tasks.maintenance()["consolidations"] == 0  # 不会重复创建
    lease = env.lease()
    assert lease["purpose"] == "consolidate"
    th = env.th(lease)
    batch = env.anon.get("/api/tools/batch", headers=th).json()
    assert [m["body"] for m in batch["messages"]] == ["第一条", "第二条"]
    assert env.anon.get("/api/tools/batch", headers=th).json()["event_to"] == lease["task"]["params"]["event_to"]
    # 没提交就完成 → 拒绝
    r = env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_committed"
    # 没有可记内容时也要提交空批次推进游标
    c = env.anon.post("/api/tools/memory/commit", headers=th, json={"event_to": batch["event_to"], "ops": []})
    assert c.status_code == 200
    assert env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={}).status_code == 200
    assert tasks.pending_event_count(env.dm) == 0
    assert tasks.maintenance()["consolidations"] == 0


def test_consolidation_disabled_room_is_skipped(env):
    env.say(env.a, env.dm, "不要整理这个")
    env.a.patch(f"/api/rooms/{env.dm}", json={"consolidation_enabled": False})
    env.cfg.consolidate_quiet = 0
    assert tasks.maintenance()["consolidations"] == 0


def test_manual_consolidation_request_and_status(env):
    env.say(env.a, env.dm, "有新内容")
    t = env.a.post(f"/api/rooms/{env.dm}/consolidate", json={}).json()["task"]
    assert t["kind"] == "consolidate" and t["status"] == "queued"
    again = env.a.post(f"/api/rooms/{env.dm}/consolidate", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_running"
    assert env.a.get(f"/api/rooms/{env.dm}/consolidation").json()["pending_events"] == 1


def test_agent_status_states(env):
    assert env.a.get("/api/agent/status").json()["status"]["state"] == "offline"
    r = env.anon.post("/api/agent/heartbeat", headers=env.cred_h, json={"adapter": "mock", "bridge_version": "0.1.0", "guide_version": tasks.GUIDE_VERSION})
    assert r.status_code == 200 and r.json()["guide_version"] == tasks.GUIDE_VERSION
    st = env.a.get("/api/agent/status").json()["status"]
    assert st["state"] == "online" and st["adapter"] == "mock"
    db.set_kv("hb_at", db.ago(3600))
    assert env.a.get("/api/agent/status").json()["status"]["state"] == "offline"
    env.a.delete(f"/api/agent/credentials/{env.cred_id}")
    assert env.a.get("/api/agent/status").json()["status"]["state"] == "unconfigured"


def test_selfcheck_triggers_full_loop(env):
    r = env.a.post("/api/agent/selfcheck").json()
    assert r["ok"] is False  # Bridge 还没上线
    env.anon.post("/api/agent/heartbeat", headers=env.cred_h, json={})
    r = env.a.post("/api/agent/selfcheck").json()
    assert r["ok"] is True and n_tasks("chat", "queued") == 1


def test_guide_and_capabilities(env):
    g = env.anon.get("/agent-guide.md")
    assert g.status_code == 200 and tasks.GUIDE_VERSION in g.text and "{{" not in g.text
    assert "aca_" not in g.text.replace("`aca_…`", "")  # 指南里没有任何真实密钥
    env.say(env.a, env.agent_dm_a, "q")
    lease = env.lease()
    caps = env.anon.get("/api/tools/capabilities", headers=env.th(lease)).json()
    assert "messages.send" in [t["name"] for t in caps["tools"]] and "memory.commit" not in [t["name"] for t in caps["tools"]]
    assert env.anon.post("/api/tools/disclosure/request", headers=env.th(lease)).status_code == 501
    assert env.anon.get("/openapi.json").status_code == 200
