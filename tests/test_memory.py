import json

from aichat import chat, db, tasks
from conftest import src


def setup_batch(env):
    m1 = env.say(env.a, env.dm, "我想夏天去海边旅行")
    m2, _ = chat.send_message(env.dm, env.agent_id, "建议青岛或三亚", "agent-1")
    m3 = env.say(env.b, env.dm, "预算 6000 以内，我觉得青岛不错")
    lease = env.consolidate_lease(env.dm)
    return m1, m2, m3, lease


def commit(env, lease, ops, summaries=None, key=None, event_to=None):
    th = env.th(lease)
    b = env.anon.get("/api/tools/batch", headers=th).json()
    body = {"event_to": event_to if event_to is not None else b["event_to"], "ops": ops, "summaries": summaries or []}
    if key:
        body["idempotency_key"] = key
    return env.anon.post("/api/tools/memory/commit", headers=th, json=body)


def topic_op(ref="t1", title="夏季旅行计划", aliases=("旅行", "出游")):
    return {"op": "topic.create", "ref": ref, "title": title, "aliases": list(aliases), "overview": "两人在讨论暑假去哪里玩"}


def human_item(env, msg, content, ref="t1", who=None, **kw):
    return {"op": "item.create", "topic": {"ref": ref}, "type": "opinion", "content": content, "author_kind": "human", "author_user_id": who or env.alice_id, "nature": "stated", "sources": [src(msg)], "reason": "t", **kw}


def agent_item(env, msg, content, ref="t1", extra_sources=(), **kw):
    return {"op": "item.create", "topic": {"ref": ref}, "type": "suggestion", "content": content, "author_kind": "agent", "author_user_id": env.agent_id, "nature": "agent_suggestion", "sources": [src(msg), *extra_sources], "reason": "t", **kw}


def topics(env, room=None):
    return env.a.get(f"/api/rooms/{room or env.dm}/topics").json()


def test_commit_creates_topic_items_summary_and_advances_cursor(env):
    m1, m2, m3, lease = setup_batch(env)
    r = commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行"), agent_item(env, m2, "青岛或三亚"), human_item(env, m3, "预算 6000 以内", who=env.bob_id)],
               summaries=[{"period_start": m1["created_at"], "period_end": m3["created_at"], "content": "聊了海边旅行和预算", "sources": [src(m1)]}])
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["items"]) == 3 and out["cursor"] == lease["task"]["params"]["event_to"]
    t = topics(env)["topics"][0]
    assert t["title"] == "夏季旅行计划" and t["item_count"] == 3
    detail = env.a.get(f"/api/topics/{t['id']}").json()["topic"]
    adv = [i for i in detail["items"] if i["author_kind"] == "agent"][0]
    assert adv["adoption"] == "none" and adv["nature"] == "agent_suggestion" and adv["sources"] == [src(m2)]
    assert env.a.get(f"/api/rooms/{env.dm}/summaries").json()["summaries"][0]["content"] == "聊了海边旅行和预算"
    assert tasks.pending_event_count(env.dm) == 0
    assert env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={}).status_code == 200


def test_commit_is_atomic_and_cursor_not_advanced_on_failure(env):
    m1, m2, m3, lease = setup_batch(env)
    bad = human_item(env, m1, "第二条会失败")
    bad["sources"] = [{"message_id": 999999, "version": 1}]
    r = commit(env, lease, [topic_op(), human_item(env, m1, "第一条本来能成功"), bad])
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_source" and r.json()["error"]["op_index"] == 2
    assert db.one("SELECT COUNT(*) AS c FROM topics")["c"] == 0 and db.one("SELECT COUNT(*) AS c FROM memory_items")["c"] == 0
    assert tasks.pending_event_count(env.dm) == 3  # 游标没有推进


def test_event_to_must_match_and_commit_is_idempotent(env):
    m1, _, _, lease = setup_batch(env)
    assert commit(env, lease, [], event_to=1).status_code == 400
    ops = [topic_op(), human_item(env, m1, "想去海边")]
    first = commit(env, lease, ops, key="k-1").json()
    second = commit(env, lease, ops, key="k-1").json()
    assert second["replayed"] is True and second["items"] == first["items"]
    assert db.one("SELECT COUNT(*) AS c FROM memory_items")["c"] == 1


def test_stale_source_after_edit_is_rejected_then_accepted_with_new_version(env):
    m1, _, _, lease = setup_batch(env)
    env.a.patch(f"/api/messages/{m1['id']}", json={"body": "我想秋天去山里旅行"})
    r = commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边")])
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_source" and r.json()["error"]["reason"] == "edited"
    fresh = env.anon.get("/api/tools/batch", headers=env.th(lease)).json()["messages"][0]
    assert fresh["version"] == 2 and fresh["body"] == "我想秋天去山里旅行"
    ok = commit(env, lease, [topic_op(), human_item(env, fresh, "想秋天去山里")])
    assert ok.status_code == 200


def test_source_from_other_room_or_deleted_is_rejected(env):
    m1, _, _, lease = setup_batch(env)
    other = env.say(env.a, env.agent_dm_a, "另一个房间里的话")
    r = commit(env, lease, [topic_op(), human_item(env, other, "不该被记进这个房间")])
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_source"
    env.a.delete(f"/api/messages/{m1['id']}")
    r = commit(env, lease, [topic_op(), human_item(env, m1, "来源已被删除")])
    assert r.status_code == 409 and r.json()["error"]["reason"] == "deleted"


def test_author_must_be_sender_of_a_source(env):
    m1, _, _, lease = setup_batch(env)
    r = commit(env, lease, [topic_op(), human_item(env, m1, "把 alice 的话记到 bob 名下", who=env.bob_id)])
    assert r.status_code == 422 and r.json()["error"]["code"] == "author_not_in_sources"
    r = commit(env, lease, [topic_op(), {**human_item(env, m1, "身份对不上"), "author_kind": "agent"}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "author_kind_mismatch"


def test_adoption_requires_a_human_source(env):
    m1, m2, m3, lease = setup_batch(env)
    r = commit(env, lease, [topic_op(), agent_item(env, m2, "建议青岛", adoption="adopted")])
    assert r.status_code == 422 and r.json()["error"]["code"] == "adoption_needs_human_source"
    # 人类条目不能带采纳状态
    r = commit(env, lease, [topic_op("t2", "别的主题", ["其他"]), human_item(env, m1, "只是个观点", ref="t2", adoption="adopted")])
    assert r.status_code == 422 and r.json()["error"]["code"] == "adoption_not_applicable"
    ok = commit(env, lease, [topic_op(), agent_item(env, m2, "建议青岛", extra_sources=[src(m3)], adoption="adopted")])
    assert ok.status_code == 200


def test_adoption_flow_across_batches_keeps_both_sources(env):
    m1, m2, m3, lease = setup_batch(env)
    commit(env, lease, [topic_op(), agent_item(env, m2, "建议青岛或三亚")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    m4 = env.say(env.a, env.dm, "那就青岛吧")
    lease2 = env.consolidate_lease(env.dm)
    item = topics(env)["topics"][0]
    detail = env.a.get(f"/api/topics/{item['id']}").json()["topic"]["items"][0]
    r = commit(env, lease2, [{"op": "item.update", "item_id": detail["id"], "expected_version": detail["version"], "adoption": "adopted", "add_sources": [src(m4)], "reason": "用户说“那就青岛吧”"}])
    assert r.status_code == 200, r.text
    now = env.a.get(f"/api/topics/{item['id']}?history=1").json()["topic"]["items"][0]
    assert now["adoption"] == "adopted" and [s["message_id"] for s in now["sources"]] == [m2["id"], m4["id"]]
    assert [h["adoption"] for h in now["history"]] == ["none", "adopted"]


def test_version_conflict_and_manual_lock(env):
    m1, m2, m3, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    tid = topics(env)["topics"][0]["id"]
    item = env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]
    # 用户手工纠正
    env.a.patch(f"/api/memory/items/{item['id']}", json={"content": "想夏天去海边（已确认，不是山里）"})
    env.say(env.b, env.dm, "补一句")
    lease2 = env.consolidate_lease(env.dm)
    stale_v = {"op": "item.update", "item_id": item["id"], "expected_version": item["version"], "content": "改回去", "reason": "x"}
    r = commit(env, lease2, [stale_v])
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict" and r.json()["error"]["current_version"] == item["version"] + 1
    fresh = env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]
    assert fresh["manual"] is True
    locked = commit(env, lease2, [{**stale_v, "expected_version": fresh["version"]}])
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "manual_correction_locked"
    # 允许补充新来源，但不能改写内容
    extra = env.anon.get("/api/tools/batch", headers=env.th(lease2)).json()["messages"][0]
    ok = commit(env, lease2, [{"op": "item.update", "item_id": item["id"], "expected_version": fresh["version"], "add_sources": [src(extra)]}])
    assert ok.status_code == 200
    assert env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]["content"].startswith("想夏天去海边（已确认")


def test_duplicate_content_links_sources_instead_of_new_item(env):
    m1, m2, m3, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想去海边")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    again = env.say(env.a, env.dm, "再说一遍，想去海边")
    lease2 = env.consolidate_lease(env.dm)
    tid = topics(env)["topics"][0]["id"]
    r = commit(env, lease2, [{**human_item(env, again, " 想去海边 "), "topic": {"id": tid}}])
    assert r.json()["skipped"][0]["reason"] == "deduplicated" and r.json()["items"] == []
    item = env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]
    assert len(item["sources"]) == 2


def test_user_deleted_memory_is_not_regenerated(env):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想去海边")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    tid = topics(env)["topics"][0]["id"]
    item = env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]
    assert env.a.delete(f"/api/memory/items/{item['id']}").status_code == 200
    # 只留下内容指纹，不保存正文
    assert db.one("SELECT COUNT(*) AS c FROM tombstones")["c"] == 1
    assert "海边" not in json.dumps([dict(r) for r in db.all("SELECT * FROM tombstones")])
    # 重放同一批事件也不会再生成
    db.run("DELETE FROM cursors")
    db.run("UPDATE agent_tasks SET status='cancelled' WHERE kind='consolidate'")
    lease2 = env.consolidate_lease(env.dm)
    r = commit(env, lease2, [{**human_item(env, m1, "想去海边"), "topic": {"id": tid}}])
    assert r.json()["skipped"][0]["reason"] == "tombstoned"
    assert env.a.get(f"/api/topics/{tid}").json()["topic"]["items"] == []


def test_edit_marks_derived_stale_and_agent_can_confirm(env):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    env.a.patch(f"/api/messages/{m1['id']}", json={"body": "我想夏天去海边旅行（预算不限）"})
    t = topics(env)["topics"][0]
    assert t["stale_count"] == 1 and t["overview_stale"] is True
    lease2 = env.consolidate_lease(env.dm)
    batch = env.anon.get("/api/tools/batch", headers=env.th(lease2)).json()
    assert len(batch["stale_items"]) == 1
    it = batch["stale_items"][0]
    r = commit(env, lease2, [
        {"op": "item.update", "item_id": it["id"], "expected_version": it["version"], "content": "想夏天去海边旅行，预算不限", "confirm_sources": True, "reason": "原文补充了预算"},
        {"op": "topic.update", "topic_id": t["id"], "expected_version": t["version"], "overview": "暑假海边旅行，预算不限"},
    ])
    assert r.status_code == 200, r.text
    t2 = topics(env)["topics"][0]
    assert t2["stale_count"] == 0 and t2["overview_stale"] is False
    now = env.a.get(f"/api/topics/{t['id']}").json()["topic"]["items"][0]
    assert now["sources"][0]["version"] == 2 and now["source_stale"] is False


def test_deleting_source_cleans_sole_source_items_and_keeps_shared_ones(env):
    m1, m2, m3, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "只来自 m1 的结论"), agent_item(env, m2, "来自 m2 和 m3 的建议", extra_sources=[src(m3)])],
           summaries=[{"content": "只依赖 m1 的回顾", "sources": [src(m1)]}])
    env.a.delete(f"/api/messages/{m1['id']}")
    tid = topics(env)["topics"][0]["id"]
    assert env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]["content"] == "来自 m2 和 m3 的建议"
    assert len(env.a.get(f"/api/topics/{tid}").json()["topic"]["items"]) == 1
    assert env.a.get(f"/api/rooms/{env.dm}/summaries").json()["summaries"] == []
    # 删除 m2 后 m3 仍支撑它：保留，但标记待核对
    env.b.delete(f"/api/messages/{m3['id']}")
    it = env.a.get(f"/api/topics/{tid}").json()["topic"]["items"][0]
    assert it["source_stale"] is True and [s["message_id"] for s in it["sources"]] == [m2["id"]]
    # 被清理内容的指纹进入 tombstone，不保存正文
    assert db.one("SELECT COUNT(*) AS c FROM tombstones")["c"] == 1


def test_topic_rules_duplicate_merge_supersede_and_pending(env):
    m1, m2, m3, lease = setup_batch(env)
    r = commit(env, lease, [topic_op("t1"), topic_op("t2", "夏季旅行计划")])
    assert r.status_code == 409 and r.json()["error"]["code"] == "topic_exists"
    ok = commit(env, lease, [
        topic_op("t1"),
        topic_op("t2", "预算", ["花费"]),
        human_item(env, m1, "想夏天去海边旅行"),
        human_item(env, m3, "预算 6000 以内", ref="t2", who=env.bob_id),
        {**human_item(env, m3, "待归类的一句话", who=env.bob_id), "topic": None},
    ])
    assert ok.status_code == 200, ok.text
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    data = topics(env)
    assert len(data["topics"]) == 2 and len(data["pending_items"]) == 1
    t1 = next(t for t in data["topics"] if t["title"] == "夏季旅行计划")
    t2 = next(t for t in data["topics"] if t["title"] == "预算")
    # 合并：旧 ID 可重定向，别名保留
    assert env.a.post(f"/api/topics/{t2['id']}/merge", json={"into_topic_id": t1["id"]}).status_code == 200
    merged = env.a.get(f"/api/topics/{t1['id']}").json()["topic"]
    assert len(merged["items"]) == 2 and "预算" in merged["aliases"]
    assert env.a.get(f"/api/topics/{t2['id']}").json()["topic"]["status"] == "merged"
    # 替代：同一人改口
    old = next(i for i in merged["items"] if "海边" in i["content"])
    env.say(env.a, env.dm, "改主意了，想去山里")
    lease2 = env.consolidate_lease(env.dm)
    new_msg = env.anon.get("/api/tools/batch", headers=env.th(lease2)).json()["messages"][-1]
    r = commit(env, lease2, [{**human_item(env, new_msg, "想去山里而不是海边"), "topic": {"id": t1["id"]}, "supersedes": [old["id"]], "reason": "alice 改口"}])
    assert r.status_code == 200
    items = env.a.get(f"/api/topics/{t1['id']}?history=1").json()["topic"]["items"]
    states = {i["content"]: i["state"] for i in items}
    assert states["想夏天去海边旅行"] == "superseded" and states["想去山里而不是海边"] == "current"


def test_human_memory_editing_endpoints(env):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想去海边")])
    tid = topics(env)["topics"][0]["id"]
    r = env.a.patch(f"/api/topics/{tid}", json={"title": "暑假出游", "aliases": ["旅行"]})
    assert r.json()["topic"]["title"] == "暑假出游" and r.json()["topic"]["version"] == 2
    made = env.b.post(f"/api/rooms/{env.dm}/memory/items", json={"topic_id": tid, "content": "Bob 手工补充：不坐飞机"}).json()["item"]
    assert made["manual"] and made["manual_origin"] and made["sources"] == []
    flagged = env.b.patch(f"/api/memory/items/{made['id']}", json={"state": "disputed"}).json()["item"]
    assert flagged["state"] == "disputed"
    hist = env.a.get(f"/api/memory/items/{made['id']}/history").json()["history"]
    assert [h["actor"] for h in hist] == [f"user:{env.bob_id}", f"user:{env.bob_id}"]
    # 非成员不能碰
    other = env.a.post("/api/rooms", json={"type": "dm", "user_id": env.carol_id}).json()["room"]["id"]
    assert env.b.patch(f"/api/memory/items/{made['id']}", json={"state": "retired"}).status_code == 200
    assert env.c.patch(f"/api/memory/items/{made['id']}", json={"state": "current"}).status_code == 404
    assert env.c.get(f"/api/topics/{tid}").status_code == 404
    assert other


def test_memory_is_isolated_between_rooms(env):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    # 同一个人与 Agent 的私聊里，看不到、搜不到、读不了群里的记忆
    env.say(env.a, env.agent_dm_a, "我们之前聊过旅行吗？")
    chat_lease = env.lease()
    th = env.th(chat_lease)
    assert env.anon.get("/api/tools/memory/search", headers=th, params={"q": "旅行 海边"}).json() == {"items": [], "topics": []}
    assert env.anon.get("/api/tools/topics", headers=th).json()["topics"] == []
    dm_topic = topics(env)["topics"][0]["id"]
    assert env.anon.get(f"/api/tools/topics/{dm_topic}", headers=th).status_code == 404
    assert env.anon.post("/api/tools/sources/verify", headers=th, json={"sources": [src(m1)]}).json()["results"][0]["reason"] == "not_in_room"
    # 同名主题在不同 Room 各自独立
    env.anon.post(f"/api/agent/tasks/{chat_lease['task']['id']}/complete", headers=env.cred_h, json={})
    m = env.say(env.a, env.agent_dm_a, "我想夏天去海边旅行，只和你说")
    lease2 = env.consolidate_lease(env.agent_dm_a)
    r = commit(env, lease2, [topic_op(), human_item(env, m, "私聊里的旅行想法")])
    assert r.status_code == 200
    assert len(topics(env, env.agent_dm_a)["topics"]) == 1 and len(topics(env)["topics"]) == 1
    assert topics(env, env.agent_dm_a)["topics"][0]["id"] != topics(env)["topics"][0]["id"]


def test_search_memory_finds_chinese_by_substring(env):
    m1, m2, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行"), agent_item(env, m2, "青岛或三亚")])
    env.anon.post(f"/api/agent/tasks/{lease['task']['id']}/complete", headers=env.cred_h, json={})
    env.say(env.a, env.dm, "@agent 我们说过去哪里旅行？")
    th = env.th(env.lease())
    found = env.anon.get("/api/tools/memory/search", headers=th, params={"q": "海边旅行"}).json()
    assert [i["content"] for i in found["items"]][0] == "想夏天去海边旅行" and found["topics"][0]["title"] == "夏季旅行计划"
    assert env.anon.get("/api/tools/memory/search", headers=th, params={"q": "完全无关的话题"}).json()["items"] == []
    by_topic = env.anon.get("/api/tools/topics", headers=th, params={"q": "出游"}).json()["topics"]
    assert by_topic and by_topic[0]["title"] == "夏季旅行计划"
