"""Bridge 集成测试：真实 HTTP 服务（独立线程）+ 真实 Bridge + 可编程适配器。
测试线程只通过 HTTP 访问服务，不直接碰数据库（服务端是单事件循环单连接）。"""
import json
import socket
import sys
import threading
import time

import httpx
import pytest
import uvicorn

from aichat import auth, chat, config, db, hub
from aichat.main import create_app
from aichat_bridge.adapters import CallbackAdapter, CommandAdapter, MockAdapter
from aichat_bridge.prompts import extract_json
from aichat_bridge.runner import Bridge


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Live:
    def __init__(self, tmp_path):
        hub.reset()
        self.app = create_app(tmp_path / "data", scheduler=True, scheduler_tick=1, consolidate_quiet=1, chat_task_ttl=60)
        self.alice = auth.create_user("alice", "Alice", "password-alice", "owner")
        self.bob = auth.create_user("bob", "Bob", "password-bob", "user")
        for u in (self.alice, self.bob):
            chat.bootstrap_user(u)
        self.agent = auth.agent_user_id()
        self.cred_id, self.cred = auth.issue_credential("live")
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        assert self.server.started
        self.a = self.client("alice", "password-alice")
        self.b = self.client("bob", "password-bob")
        rooms = self.a.get("/api/rooms").json()["rooms"]
        self.dm = next(r["id"] for r in rooms if r["type"] == "dm")
        self.agent_dm = next(r["id"] for r in rooms if r["type"] == "agent_dm")

    def client(self, user, pw):
        c = httpx.Client(base_url=self.base, timeout=20)
        assert c.post("/api/login", json={"username": user, "password": pw}).status_code == 200
        return c

    def bridge(self, adapter, **kw):
        return Bridge(self.base, self.cred, adapter, owner="test", **kw)

    def say(self, client, room, body, **kw):
        r = client.post(f"/api/rooms/{room}/messages", json={"body": body, **kw})
        assert r.status_code == 200, r.text
        return r.json()["message"]

    def msgs(self, client, room):
        return client.get(f"/api/rooms/{room}/messages").json()["messages"]

    def wait(self, fn, timeout=15, step=0.2):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(step)
        raise AssertionError("等待超时")

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        db.close()


@pytest.fixture
def live(tmp_path):
    lv = Live(tmp_path)
    yield lv
    lv.close()


def test_chat_roundtrip_through_real_bridge(live):
    q = live.say(live.a, live.agent_dm, "今天吃什么？")
    br = live.bridge(CallbackAdapter(chat_fn=lambda ctx: f"建议吃面。（你问的是：{ctx['trigger']['body']}）"))
    br.heartbeat()
    assert br.run_once(wait=3) is True
    msgs = live.msgs(live.a, live.agent_dm)
    reply = msgs[-1]
    assert reply["kind"] == "agent" and reply["reply_to"] == q["id"] and "建议吃面" in reply["body"]
    tasks = live.a.get(f"/api/rooms/{live.agent_dm}/tasks").json()["tasks"]
    assert tasks[0]["status"] == "done"
    st = live.a.get("/api/agent/status").json()["status"]
    assert st["state"] == "online" and st["adapter"] == "callback"
    # Agent 的回复不会再触发新任务
    assert br.run_once(wait=0) is False


def test_adapter_sees_only_current_room_context(live):
    live.say(live.a, live.dm, "这是 alice 和 bob 的私聊秘密：暗号是香蕉")
    live.say(live.a, live.agent_dm, "你好")
    seen = {}
    br = live.bridge(CallbackAdapter(chat_fn=lambda ctx: seen.update(ctx) or "ok"))
    br.heartbeat()
    br.run_once(wait=3)
    flat = json.dumps(seen, ensure_ascii=False)
    assert "香蕉" not in flat and "你好" in flat


def test_long_poll_wakes_immediately_on_new_task(live):
    br = live.bridge(CallbackAdapter(chat_fn=lambda ctx: "收到"))
    got = {}

    def poll():
        t0 = time.time()
        got["lease"] = br.lease(wait=20)
        got["dt"] = time.time() - t0

    t = threading.Thread(target=poll)
    t.start()
    time.sleep(1.0)
    live.say(live.a, live.agent_dm, "叫醒你")
    t.join(timeout=10)
    assert got["lease"] and got["lease"]["purpose"] == "chat"
    assert got["dt"] < 4, "长轮询应当在任务创建后立即返回，而不是等到超时"


def test_adapter_failure_retries_then_marks_failed(live):
    live.say(live.a, live.agent_dm, "会失败的请求")
    calls = {"n": 0}

    def boom(ctx):
        calls["n"] += 1
        raise RuntimeError("模型服务 503")

    br = live.bridge(CallbackAdapter(chat_fn=boom))
    br.heartbeat()
    for _ in range(3):
        assert br.run_once(wait=2) is True
    assert br.run_once(wait=0) is False and calls["n"] == 3
    t = live.a.get(f"/api/rooms/{live.agent_dm}/tasks").json()["tasks"][0]
    assert t["status"] == "failed" and "503" in t["error"]
    # 用户可以重试，之后恢复正常
    new = live.a.post(f"/api/tasks/{t['id']}/retry").json()["task"]
    assert new["status"] == "queued"
    ok = live.bridge(CallbackAdapter(chat_fn=lambda ctx: "这次好了"))
    assert ok.run_once(wait=3)
    assert live.msgs(live.a, live.agent_dm)[-1]["body"] == "这次好了"
    assert "503" in (live.a.get("/api/agent/status").json()["status"]["last_error"] or {}).get("error", "")


def test_credential_revoked_mid_task_is_abandoned_without_reply(live):
    live.say(live.a, live.agent_dm, "你在处理时凭证被撤销")

    def revoke_then_answer(ctx):
        assert live.a.delete(f"/api/agent/credentials/{live.cred_id}").status_code == 200
        return "不该被发出的回复"

    br = live.bridge(CallbackAdapter(chat_fn=revoke_then_answer))
    br.heartbeat()
    br.run_once(wait=3)
    assert all(m["kind"] != "agent" for m in live.msgs(live.a, live.agent_dm))


def test_deleting_question_while_agent_is_thinking_drops_reply(live):
    q = live.say(live.a, live.agent_dm, "我马上会删掉这条")

    def delete_then_answer(ctx):
        assert live.a.delete(f"/api/messages/{q['id']}").status_code == 200
        return "回复一条已删除的消息"

    br = live.bridge(CallbackAdapter(chat_fn=delete_then_answer))
    br.heartbeat()
    br.run_once(wait=3)
    assert all(m["kind"] != "agent" for m in live.msgs(live.a, live.agent_dm))


def test_consolidation_through_bridge_and_stale_retry(live):
    m1 = live.say(live.a, live.dm, "我想夏天去海边旅行")
    m2 = live.say(live.b, live.dm, "预算 6000 以内")
    state = {"calls": 0, "errors": []}

    def plan(ctx):
        state["calls"] += 1
        state["errors"].append(ctx.get("previous_error"))
        b = ctx["batch"]
        if state["calls"] == 1:
            # 模拟：整理过程中有人改了原文 → 本次提交会因为来源版本过期被拒绝
            assert live.a.patch(f"/api/messages/{m1['id']}", json={"body": "我想夏天去海边旅行（改成了秋天去山里）"}).status_code == 200
        by_id = {m["id"]: m for m in b["messages"]}
        first, second = by_id[m1["id"]], by_id[m2["id"]]
        return {
            "summaries": [{"content": "聊了旅行和预算", "sources": [{"message_id": first["id"], "version": first["version"]}]}],
            "ops": [
                {"op": "topic.create", "ref": "t1", "title": "夏季旅行计划", "aliases": ["旅行"], "overview": "讨论暑假出行"},
                {"op": "item.create", "topic": {"ref": "t1"}, "type": "opinion", "content": first["body"], "author_kind": "human", "author_user_id": first["sender_id"], "nature": "stated", "sources": [{"message_id": first["id"], "version": first["version"]}], "reason": "alice 的想法"},
                {"op": "item.create", "topic": {"ref": "t1"}, "type": "preference", "content": "预算 6000 以内", "author_kind": "human", "author_user_id": second["sender_id"], "nature": "stated", "sources": [{"message_id": second["id"], "version": second["version"]}], "reason": "bob 的预算"},
            ],
        }

    br = live.bridge(CallbackAdapter(consolidate_fn=plan))
    br.heartbeat()
    live.wait(lambda: live.a.get(f"/api/rooms/{live.dm}/consolidation").json()["active"] is not None, timeout=20)
    assert br.run_once(wait=5) is True
    assert state["calls"] == 2 and "stale_source" in state["errors"][1]
    topics = live.a.get(f"/api/rooms/{live.dm}/topics").json()["topics"]
    assert topics[0]["title"] == "夏季旅行计划" and topics[0]["item_count"] == 2
    items = live.a.get(f"/api/topics/{topics[0]['id']}").json()["topic"]["items"]
    assert any("秋天去山里" in i["content"] for i in items)
    # 整理 Bridge 之外的一切读取都限定在本会话
    assert live.a.get(f"/api/rooms/{live.agent_dm}/topics").json()["topics"] == []
    assert live.a.get(f"/api/rooms/{live.dm}/consolidation").json()["pending_events"] >= 1  # 编辑产生的新事件等待下一批


def test_empty_batch_still_advances_cursor(live):
    live.say(live.a, live.dm, "嗨")
    br = live.bridge(CallbackAdapter(consolidate_fn=lambda ctx: {"summaries": [], "ops": []}))
    br.heartbeat()
    live.wait(lambda: live.a.get(f"/api/rooms/{live.dm}/consolidation").json()["active"] is not None, timeout=20)
    assert br.run_once(wait=5)
    st = live.a.get(f"/api/rooms/{live.dm}/consolidation").json()
    assert st["pending_events"] == 0 and st["last"]["status"] == "done"


def test_mock_adapter_end_to_end_adoption_flow(live):
    live.say(live.a, live.dm, "我想夏天去海边旅行")
    live.say(live.a, live.dm, "@agent 有什么建议？")
    br = live.bridge(MockAdapter())
    br.heartbeat()
    assert br.run_once(wait=3)  # 聊天回复
    live.say(live.b, live.dm, "那就青岛吧，大家都同意")
    live.wait(lambda: live.a.get(f"/api/rooms/{live.dm}/consolidation").json()["active"] is not None, timeout=20)
    assert br.run_once(wait=5)
    topics = live.a.get(f"/api/rooms/{live.dm}/topics").json()
    flat = [i for t in topics["topics"] for i in live.a.get(f"/api/topics/{t['id']}").json()["topic"]["items"]] + topics["pending_items"]
    assert flat, "mock 适配器也应整理出条目"
    assert all(i["sources"] for i in flat)


def test_guide_is_synced_and_version_reported(live, tmp_path):
    guide = tmp_path / "AGENT_GUIDE.md"
    br = live.bridge(CallbackAdapter(), guide_path=str(guide))
    br.heartbeat()
    assert guide.exists() and "AGENT_GUIDE" in guide.read_text(encoding="utf-8")
    assert live.a.get("/api/agent/status").json()["status"]["guide_version_agent"] == br.guide_version


def test_websocket_realtime_across_users_on_real_server(live):
    from websockets.sync.client import connect

    cookie = "; ".join(f"{k}={v}" for k, v in live.b.cookies.items())
    with connect(f"ws://127.0.0.1:{live.port}/ws", additional_headers={"Cookie": cookie, "Origin": live.base}) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "hello"
        live.say(live.a, live.dm, "来自 Alice 的实时消息")
        evt = json.loads(ws.recv(timeout=5))
        assert evt["type"] == "message.created" and evt["message"]["body"] == "来自 Alice 的实时消息"
        # 触发 Agent 任务的消息会带来任务状态推送（仅发给房间里的真人）
        live.say(live.a, live.dm, "@agent 在吗")
        types = []
        for _ in range(3):
            types.append(json.loads(ws.recv(timeout=5))["type"])
        assert "task.updated" in types and "message.created" in types


# ---------------------------------------------------------------- 适配器与解析
def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"ops": []}\n```') == {"ops": []}
    assert extract_json('好的，这是结果：\n{"ops": [1]}\n希望有帮助') == {"ops": [1]}
    with pytest.raises(ValueError):
        extract_json("没有 JSON")


def test_command_adapter_runs_in_fresh_dir_and_parses_output(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(
        "import sys, os\n"
        "prompt = sys.stdin.read()\n"
        "if '整理者' in prompt:\n"
        "    print('```json'); print('{\"summaries\": [], \"ops\": []}'); print('```')\n"
        "else:\n"
        "    print('命令行回复，工作目录为空：', os.listdir('.') == [])\n"
    )
    ad = CommandAdapter(f"{sys.executable} {script}")
    out = ad.chat({"members": [{"id": 1, "display_name": "A", "role": "user"}], "history": [], "trigger": {"id": 1, "sender_id": 1, "kind": "human", "created_at": "2026-10-28T10:00:00Z", "version": 1, "body": "hi", "deleted": False}, "memory": None})
    assert "命令行回复" in out and "True" in out
    plan = ad.consolidate({"batch": {"members": [{"id": 1, "display_name": "A", "role": "user"}], "stale_items": [], "context_before": [], "messages": [], "mode": "pending", "event_from": 0, "event_to": 1}, "topics": [], "items": []})
    assert plan == {"summaries": [], "ops": []}
    bad = CommandAdapter(f"{sys.executable} -c 'import sys; sys.exit(3)'")
    with pytest.raises(RuntimeError):
        bad.complete("s", "u")


# ------------------------------------------------------------------ 图片 / 语音转写 / 纪要存档
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
WEBM = b"\x1a\x45\xdf\xa3" + b"0" * 200


def _upload(client, room, name, data, mime, **form):
    r = client.post(f"/api/rooms/{room}/attachments", files={"file": (name, data, mime)}, data=form)
    assert r.status_code == 200, r.text
    return r.json()["attachment"]


def test_agent_receives_recent_images(live):
    att = _upload(live.a, live.agent_dm, "pic.png", PNG, "image/png")
    live.say(live.a, live.agent_dm, "这张图是什么", attachment_id=att["id"])
    got = {}

    def chat_fn(ctx):
        got["images"] = ctx["images"]
        return "看到了"

    br = live.bridge(CallbackAdapter(chat_fn=chat_fn))
    br.heartbeat()
    assert br.run_once(wait=3) is True
    assert len(got["images"]) == 1 and got["images"][0]["mime"] == "image/png"
    import base64

    assert base64.b64decode(got["images"][0]["b64"]) == PNG


def test_tool_attachment_is_scoped_to_task_room(live):
    other = live.a.post("/api/rooms", json={"type": "dm", "user_id": live.bob, "include_agent": False}).json()["room"]["id"] if False else live.dm
    att = _upload(live.a, live.dm, "pic.png", PNG, "image/png")
    live.say(live.a, live.dm, "x", attachment_id=att["id"])
    txt = _upload(live.a, live.agent_dm, "n.txt", b"hello", "text/plain")
    live.say(live.a, live.agent_dm, "文件", attachment_id=txt["id"])
    seen = {}

    def chat_fn(ctx):
        return "ok"

    br = live.bridge(CallbackAdapter(chat_fn=chat_fn))
    br.heartbeat()
    lease = br.http.call("POST", "/api/agent/lease", br.cred)["lease"]
    tok = lease["task_token"]
    # 别的房间里的图片：读不到；非图片非语音：不给
    r = httpx.get(f"{live.base}/api/tools/attachments/{att['id']}", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404
    r = httpx.get(f"{live.base}/api/tools/attachments/{txt['id']}", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 415
    br.http.call("POST", f"/api/agent/tasks/{lease['task']['id']}/fail", br.cred, {"error": "x", "retry": False})


class _SttAdapter(CallbackAdapter):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.stt_calls = []

    def transcribe(self, data, filename, mime):
        self.stt_calls.append((len(data), filename, mime))
        return "明天下午三点开会"


def test_transcribe_voice_writes_back_without_chat_message(live):
    att = _upload(live.a, live.agent_dm, "voice-1.webm", WEBM, "audio/webm", duration_ms="3000")
    m = live.say(live.a, live.agent_dm, "", attachment_id=att["id"])
    # 私聊 Agent 时语音本身会触发一次聊天任务：先让它处理掉
    ad = _SttAdapter(chat_fn=lambda ctx: "收到语音")
    br = live.bridge(ad)
    br.heartbeat()
    assert br.run_once(wait=3) is True
    n_before = len(live.msgs(live.a, live.agent_dm))
    r = live.a.post(f"/api/messages/{m['id']}/transcribe")
    assert r.status_code == 200 and r.json()["task"]["status"] == "queued"
    # 重复点击不会重复建任务
    assert live.a.post(f"/api/messages/{m['id']}/transcribe").json()["task"]["id"] == r.json()["task"]["id"]
    assert br.run_once(wait=3) is True
    assert ad.stt_calls and ad.stt_calls[0][1].startswith("voice-")
    after = live.msgs(live.a, live.agent_dm)
    assert len(after) == n_before  # 没有新的聊天消息
    voice = next(x for x in after if x["id"] == m["id"])
    assert voice["attachment"]["transcript"] == "明天下午三点开会"
    # 文字消息不能转写
    t = live.say(live.a, live.agent_dm, "普通消息")
    assert live.a.post(f"/api/messages/{t['id']}/transcribe").status_code == 400


def test_summary_command_archives_to_notion(live, monkeypatch):
    from aichat_bridge import notion

    live.say(live.a, live.dm, "我们讨论一下预算")
    live.say(live.b, live.dm, "我觉得 5 万够了")
    calls = []
    monkeypatch.setattr(notion, "enabled", lambda: True)
    monkeypatch.setattr(notion, "create_page", lambda title, md: (calls.append((title, md)), "https://notion.example/p1")[1])
    seen = {}

    def chat_fn(ctx):
        seen["command"] = ctx.get("command")
        seen["history_n"] = len(ctx["history"])
        return "# 预算讨论\n## 主要观点\n- 5 万够了"

    live.say(live.a, live.dm, "@Agent /总结存档")
    br = live.bridge(CallbackAdapter(chat_fn=chat_fn))
    br.heartbeat()
    assert br.run_once(wait=3) is True
    assert seen["command"] == "summary" and seen["history_n"] >= 2
    reply = live.msgs(live.a, live.dm)[-1]["body"]
    assert "已存档到 Notion：https://notion.example/p1" in reply
    assert calls and calls[0][0].startswith("预算讨论（") and "5 万够了" in calls[0][1]


def test_notion_blocks_conversion():
    from aichat_bridge import notion

    b = notion.to_blocks("# 标题\n## 小节\n- 要点\n1. 步骤\n正文\n\n" + "长" * 4000)
    assert [x["type"] for x in b] == ["heading_1", "heading_2", "bulleted_list_item", "numbered_list_item", "paragraph", "paragraph"]
    assert len(b[-1]["paragraph"]["rich_text"]) == 3  # 4000 字被切成 ≤1900 的几段
