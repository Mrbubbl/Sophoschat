"""表情回应、语音消息、多设备会话、离线推送、数据库升级。"""
import asyncio

from aichat import auth, db, push


# ------------------------------------------------------------------ 表情回应
def test_reactions_toggle_and_visibility(env):
    m = env.say(env.a, env.dm, "你好")
    r = env.b.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍"})
    assert r.status_code == 200 and r.json()["reactions"] == [{"emoji": "👍", "user_ids": [env.bob_id]}]
    env.a.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍"})
    got = env.a.get(f"/api/rooms/{env.dm}/messages").json()["messages"][-1]
    assert got["reactions"][0]["user_ids"] == [env.bob_id, env.alice_id]
    # 重复点同一个是幂等的；取消只取消自己的
    env.b.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍"})
    env.b.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍", "on": False})
    got = env.a.get(f"/api/rooms/{env.dm}/messages").json()["messages"][-1]
    assert got["reactions"][0]["user_ids"] == [env.alice_id]
    # ZWJ 序列可用；纯文字、空串、过长不行
    assert env.a.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👨‍👩‍👧"}).status_code == 200
    for bad in ("ok", "", " ", "x" * 40):
        assert env.a.post(f"/api/messages/{m['id']}/reactions", json={"emoji": bad}).status_code == 400
    # 非成员不能回应；删除消息后回应一并清除
    carol_dm = env.a.post("/api/rooms", json={"type": "dm", "user_id": env.carol_id}).json()["room"]["id"]
    assert env.c.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍"}).status_code in (403, 404)
    env.a.delete(f"/api/messages/{m['id']}")
    assert db.one("SELECT COUNT(*) n FROM reactions WHERE message_id=?", (m["id"],))["n"] == 0
    assert env.b.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "👍"}).status_code == 404
    assert carol_dm


def test_reaction_limit(env):
    m = env.say(env.a, env.dm, "x")
    for e in "😀😁😂🤣😃😄😅😆😉😊😋😎":
        assert env.a.post(f"/api/messages/{m['id']}/reactions", json={"emoji": e}).status_code == 200
    assert env.a.post(f"/api/messages/{m['id']}/reactions", json={"emoji": "😍"}).status_code == 400


# ------------------------------------------------------------------ 语音
def test_voice_message(env):
    webm = b"\x1a\x45\xdf\xa3" + b"0" * 200
    r = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("voice-1.webm", webm, "audio/webm;codecs=opus")}, data={"duration_ms": "4200"})
    att = r.json()["attachment"]
    assert att["is_voice"] and att["duration_ms"] == 4200
    m = env.say(env.a, env.dm, "", attachment_id=att["id"])
    d = env.b.get(m["attachment"]["url"])
    assert d.status_code == 200 and d.headers["content-disposition"].startswith("inline") and d.headers["content-type"].startswith("audio/webm")
    assert d.headers["x-content-type-options"] == "nosniff"
    # 声称是音频但内容不是：按普通文件，强制下载，不算语音
    fake = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("v.webm", b"<html>", "audio/webm")}, data={"duration_ms": "1000"}).json()["attachment"]
    assert not fake["is_voice"]
    env.say(env.a, env.dm, "", attachment_id=fake["id"])
    assert env.b.get(fake["url"]).headers["content-disposition"].startswith("attachment")
    # 没有时长的音频文件（比如从相册选的 mp3）当普通文件
    mp3 = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("s.mp3", b"ID3" + b"0" * 50, "audio/mpeg")}).json()["attachment"]
    assert not mp3["is_voice"]
    # 时长上限
    big = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("v.webm", webm, "audio/webm")}, data={"duration_ms": "99999999"}).json()["attachment"]
    assert big["duration_ms"] == 15 * 60 * 1000


# ------------------------------------------------------------------ 多设备会话
def test_sessions_list_and_revoke(env):
    from fastapi.testclient import TestClient

    phone = TestClient(env.app)
    phone.post("/api/login", json={"username": "alice", "password": "password-alice"}, headers={"user-agent": "Mozilla/5.0 (Linux; Android 15; SM-S938B) Chrome/130 Mobile Safari/537.36"})
    s = env.a.get("/api/sessions").json()["sessions"]
    assert len(s) == 2 and sum(1 for x in s if x["current"]) == 1
    phone_s = next(x for x in s if "Android" in x["device"])
    assert phone_s["device"] == "Chrome · Android" and not phone_s["current"]
    # 手机会话可用 → 被电脑端踢下线后立即失效
    assert phone.get("/api/me").status_code == 200
    assert env.a.delete(f"/api/sessions/{phone_s['id']}").json()["revoked"] == 1
    assert phone.get("/api/me").status_code == 401
    # 不能通过这个接口踢掉当前设备；也不能踢别人的会话
    cur = next(x for x in env.a.get("/api/sessions").json()["sessions"] if x["current"])
    assert env.a.delete(f"/api/sessions/{cur['id']}").json()["revoked"] == 0
    bob_sid = env.b.get("/api/sessions").json()["sessions"][0]["id"]
    assert env.a.delete(f"/api/sessions/{bob_sid}").json()["revoked"] == 0
    assert env.b.get("/api/me").status_code == 200
    # 退出其他所有设备
    p2 = TestClient(env.app)
    p2.post("/api/login", json={"username": "alice", "password": "password-alice"})
    assert env.a.post("/api/sessions/revoke-others").json()["revoked"] == 1
    assert p2.get("/api/me").status_code == 401 and env.a.get("/api/me").status_code == 200


def test_revoked_session_closes_websocket(env):
    from fastapi.testclient import TestClient

    other = TestClient(env.app)
    other.post("/api/login", json={"username": "alice", "password": "password-alice"})
    sid = next(x for x in env.a.get("/api/sessions").json()["sessions"] if not x["current"])["id"]
    with other.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        env.a.delete(f"/api/sessions/{sid}")
        assert ws.receive_json()["type"] == "session.revoked"


# ------------------------------------------------------------------ 正在输入
def test_typing_is_relayed_to_other_members_only(env):
    from aichat import chat, hub

    class FakeWs:
        def __init__(self):
            self.got = []

        async def send_json(self, d):
            self.got.append(d)

    async def run():
        wa, wb = FakeWs(), FakeWs()
        await hub.connect(env.alice_id, wa)
        await hub.connect(env.bob_id, wb)
        chat.typing(env.dm, env.alice_id)
        chat.typing(env.dm, env.alice_id)  # 2 秒内的重复被丢弃
        # 非成员伪造房间号：静默忽略
        other = env.c.post("/api/rooms", json={"type": "dm", "user_id": env.bob_id}).json()["room"]["id"]
        chat.typing(other, env.alice_id)
        await asyncio.sleep(0.05)
        return wa.got, wb.got

    got_a, got_b = asyncio.run(run())
    assert got_a == []
    assert got_b == [{"type": "typing", "room_id": env.dm, "user_id": env.alice_id}]


# ------------------------------------------------------------------ 离线推送
def test_push_subscribe_and_notify(env, monkeypatch):
    assert push.AVAILABLE
    key = env.a.get("/api/push/key").json()
    assert key["available"] and len(key["key"]) > 60 and key["subscribed"] == 0
    assert env.a.get("/api/push/key").json()["key"] == key["key"]  # 稳定
    sub = {"endpoint": "https://push.example/abc", "p256dh": "BPk", "auth": "au"}
    assert env.b.post("/api/push/subscribe", json=sub).status_code == 200
    assert env.b.post("/api/push/subscribe", json={**sub, "endpoint": "http://insecure"}).status_code == 400
    sent = []

    def fake(subs, payload):
        sent.append((subs, payload))
        return []

    monkeypatch.setattr(push, "_send_blocking", fake)

    async def drain():
        await asyncio.sleep(0.05)

    env.say(env.a, env.dm, "在吗")
    env.app.router  # noqa
    import time

    for _ in range(20):
        if sent:
            break
        time.sleep(0.05)
    # TestClient 的事件循环在线程里，给它一点时间
    assert sent, "应当向 Bob 的订阅发送推送"
    subs, payload = sent[0]
    assert [s["endpoint"] for s in subs] == ["https://push.example/abc"]
    assert '"title": "Alice"' in payload and "在吗" in payload


def test_push_skips_sender_and_muted(env, monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_send_blocking", lambda subs, payload: (sent.append(subs), [])[1])
    sub = {"endpoint": "https://push.example/a", "p256dh": "k", "auth": "a"}
    env.a.post("/api/push/subscribe", json=sub)
    env.b.post("/api/push/subscribe", json={**sub, "endpoint": "https://push.example/b"})
    env.b.post(f"/api/rooms/{env.dm}/mute", json={"muted": True})
    env.say(env.a, env.dm, "静音测试")
    import time

    time.sleep(0.4)
    assert sent == []  # 自己不推；Bob 静音了


def test_push_removes_dead_subscription(env, monkeypatch):
    monkeypatch.setattr(push, "_send_blocking", lambda subs, payload: [s["endpoint"] for s in subs])
    env.b.post("/api/push/subscribe", json={"endpoint": "https://push.example/dead", "p256dh": "k", "auth": "a"})
    env.say(env.a, env.dm, "hi")
    import time

    for _ in range(20):
        if push.count_for(env.bob_id) == 0:
            break
        time.sleep(0.05)
    assert push.count_for(env.bob_id) == 0


def test_push_payload_for_voice_and_group():
    p = push.build_payload({"id": 5, "type": "group", "title": "项目"}, "Bob", {"id": 9, "body": None, "attachment": {"is_voice": True}})
    assert p["title"] == "项目 · Bob" and p["body"] == "[语音]" and p["tag"] == "room-5"


# ------------------------------------------------------------------ 旧库升级
def test_migration_from_v1(env):
    env.say(env.a, env.dm, "升级前的消息")
    c = db.conn()
    for t, col in (("sessions", "last_seen_at"), ("sessions", "ip"), ("attachments", "duration_ms"), ("attachments", "transcript"), ("users", "avatar"), ("users", "can_add_users")):
        c.execute(f"ALTER TABLE {t} DROP COLUMN {col}")
    c.execute("DROP TABLE reactions")
    c.execute("DROP TABLE push_subscriptions")
    c.execute("PRAGMA user_version=2")  # 线上已经是 2：新增的列也必须被补上
    n_msgs = db.one("SELECT COUNT(*) n FROM messages")["n"]
    path = env.cfg.data_dir / "aichat.db"
    db.connect(path)
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in db.conn().execute("PRAGMA table_info(sessions)")}
    assert {"last_seen_at", "ip"} <= cols
    assert "avatar" in {r[1] for r in db.conn().execute("PRAGMA table_info(users)")}
    assert "transcript" in {r[1] for r in db.conn().execute("PRAGMA table_info(attachments)")}
    assert db.one("SELECT COUNT(*) n FROM messages")["n"] == n_msgs
    db.conn().execute("SELECT * FROM reactions").fetchall()


# ------------------------------------------------------------------ 通话信令
class _FakeWs:
    def __init__(self):
        self.got = []

    async def send_json(self, d):
        self.got.append(d)


def _run_calls(env, steps):
    from aichat import calls, hub

    async def go():
        wa, wb = _FakeWs(), _FakeWs()
        await hub.connect(env.alice_id, wa)
        await hub.connect(env.bob_id, wb)
        steps(calls, env)
        await asyncio.sleep(0.05)
        return [x for x in wa.got if x["type"] == "call.signal"], [x for x in wb.got if x["type"] == "call.signal"]

    return asyncio.run(go())


def test_call_invite_accept_hangup_logs_duration(env, monkeypatch):
    import time as _t

    def steps(calls, env):
        calls.handle(env.alice_id, {"kind": "invite", "room_id": env.dm, "call_id": "c1", "media": "video"})
        calls.handle(env.bob_id, {"kind": "accept", "room_id": env.dm, "call_id": "c1"})
        calls._calls["c1"]["accepted"] -= 75  # 通话 75 秒
        calls.handle(env.alice_id, {"kind": "offer", "room_id": env.dm, "call_id": "c1", "data": {"sdp": "x"}})
        calls.handle(env.bob_id, {"kind": "hangup", "room_id": env.dm, "call_id": "c1"})

    a, b = _run_calls(env, steps)
    assert [x["kind"] for x in b] == ["invite", "offer"] and b[0]["media"] == "video" and b[1]["data"] == {"sdp": "x"}
    assert [x["kind"] for x in a] == ["accept", "hangup"]
    last = env.a.get(f"/api/rooms/{env.dm}/messages").json()["messages"][-1]
    assert last["body"] == "[通话] 视频通话 · 1:15" and last["sender_id"] == env.alice_id


def test_call_declined_busy_and_missed_logs(env):
    def steps(calls, env):
        calls.handle(env.alice_id, {"kind": "invite", "room_id": env.dm, "call_id": "d1"})
        # 通话进行中再来一个邀请 → 忙线
        calls.handle(env.bob_id, {"kind": "invite", "room_id": env.dm, "call_id": "d2"})
        calls.handle(env.bob_id, {"kind": "decline", "room_id": env.dm, "call_id": "d1"})
        calls.handle(env.alice_id, {"kind": "invite", "room_id": env.dm, "call_id": "d3"})
        calls.handle(env.alice_id, {"kind": "cancel", "room_id": env.dm, "call_id": "d3"})

    a, b = _run_calls(env, steps)
    assert any(x["kind"] == "busy" and x["call_id"] == "d2" for x in b)
    msgs = [m["body"] for m in env.a.get(f"/api/rooms/{env.dm}/messages").json()["messages"] if (m["body"] or "").startswith("[通话]")]
    assert msgs == ["[通话] 语音通话被对方拒绝", "[通话] 未接通的语音通话"]


def test_call_rejects_non_dm_and_outsiders(env):
    group = env.a.post("/api/rooms", json={"type": "group", "title": "g", "member_ids": [env.bob_id, env.carol_id]}).json()["room"]["id"]

    def steps(calls, env):
        calls.handle(env.alice_id, {"kind": "invite", "room_id": group, "call_id": "g1"})
        calls.handle(env.carol_id, {"kind": "invite", "room_id": env.dm, "call_id": "x1"})  # carol 不在 alice-bob 的会话里
        calls.handle(env.alice_id, {"kind": "bogus", "room_id": env.dm, "call_id": "z"})

    a, b = _run_calls(env, steps)
    assert [x["kind"] for x in a] == ["unsupported"] and b == []


def test_call_ends_when_user_disconnects(env):
    def steps(calls, env):
        calls.handle(env.alice_id, {"kind": "invite", "room_id": env.dm, "call_id": "e1"})
        calls.on_disconnect(env.alice_id)

    a, b = _run_calls(env, steps)
    assert [x["kind"] for x in b] == ["invite", "hangup"]


def test_ice_config_turn_credentials(env, monkeypatch):
    import base64, hashlib, hmac

    cfg = env.a.get("/api/call/config").json()
    assert cfg["turn"] is False and cfg["ice_servers"]
    monkeypatch.setenv("AICHAT_TURN_URL", "turn:t.example:3478,turns:t.example:5349")
    monkeypatch.setenv("AICHAT_TURN_SECRET", "s3cret")
    cfg = env.a.get("/api/call/config").json()
    t = cfg["ice_servers"][-1]
    assert cfg["turn"] and t["urls"] == ["turn:t.example:3478", "turns:t.example:5349"]
    assert t["credential"] == base64.b64encode(hmac.new(b"s3cret", t["username"].encode(), hashlib.sha1).digest()).decode()
    assert t["username"].endswith(f":{env.alice_id}")
    assert env.anon.get("/api/call/config").status_code == 401


def test_static_files_always_revalidate(env):
    for path in ("/static/chatview.js", "/static/style.css", "/static/call.js", "/", "/sw.js"):
        r = env.anon.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    etag = env.anon.get("/static/chatview.js").headers.get("etag")
    assert etag and env.anon.get("/static/chatview.js", headers={"if-none-match": etag}).status_code == 304


# ------------------------------------------------------------------ 头像
PNG1 = b"\x89PNG\r\n\x1a\n" + b"0" * 80


def test_avatar_upload_serve_and_visibility(env):
    r = env.a.post("/api/me/avatar", files={"file": ("a.png", PNG1, "image/png")})
    assert r.status_code == 200
    url = r.json()["user"]["avatar_url"]
    assert url.startswith(f"/api/avatars/{env.alice_id}?v=")
    g = env.b.get(url)  # 其他成员能看到
    assert g.status_code == 200 and g.content == PNG1 and g.headers["content-type"] == "image/png"
    assert "immutable" in g.headers["cache-control"] and g.headers["x-content-type-options"] == "nosniff"
    assert env.anon.get(url).status_code == 401
    # 房间成员和用户列表里带头像地址
    room = env.b.get(f"/api/rooms/{env.dm}").json()["room"]
    assert any(m["id"] == env.alice_id and m["avatar_url"] == url for m in room["members"])
    assert any(u["id"] == env.alice_id and u["avatar_url"] == url for u in env.b.get("/api/users").json()["users"])
    # 换图 → 地址里的版本号变化；删除 → 回到默认
    r2 = env.a.post("/api/me/avatar", files={"file": ("b.png", PNG1 + b"1", "image/png")})
    assert r2.json()["user"]["avatar_url"] != url
    assert env.a.delete("/api/me/avatar").json()["user"]["avatar_url"] is None
    assert env.b.get(url).status_code == 404


def test_avatar_rejects_non_images_and_large(env):
    assert env.a.post("/api/me/avatar", files={"file": ("a.png", b"<html>x</html>", "image/png")}).status_code == 400
    assert env.a.post("/api/me/avatar", files={"file": ("a.png", PNG1 + b"0" * (2 * 1024 * 1024), "image/png")}).status_code == 413


def test_only_owner_sets_agent_avatar(env):
    assert env.b.post("/api/agent/avatar", files={"file": ("a.png", PNG1, "image/png")}).status_code == 403
    r = env.a.post("/api/agent/avatar", files={"file": ("a.png", PNG1, "image/png")})
    assert r.status_code == 200 and r.json()["user"]["role"] == "agent" and r.json()["user"]["avatar_url"]
    members = env.b.get(f"/api/rooms/{env.agent_dm_b}").json()["room"]["members"]
    assert next(m for m in members if m["role"] == "agent")["avatar_url"]


def test_backup_includes_avatars_and_vapid_key(env, tmp_path):
    import tarfile

    from aichat import backup

    env.a.post("/api/me/avatar", files={"file": ("a.png", PNG1, "image/png")})
    env.a.get("/api/push/key")  # 生成 VAPID 密钥
    backup.create_backup(tmp_path / "b.tgz")
    names = tarfile.open(tmp_path / "b.tgz").getnames()
    assert any(n.startswith("avatars/") for n in names) and "vapid_private.pem" in names


# ------------------------------------------------------------------ 添加成员的权限
def test_owner_can_delegate_adding_members(env):
    new = {"username": "dave", "display_name": "Dave", "password": "password-dave"}
    # 普通成员默认不能加人
    assert env.b.post("/api/users", json=new).status_code == 403
    assert env.b.get("/api/me").json()["user"]["can_add_users"] is False
    # 只有 Owner 能授权；被授权者自己不能改权限
    assert env.b.patch(f"/api/users/{env.carol_id}/permissions", json={"can_add_users": True}).status_code == 403
    r = env.a.patch(f"/api/users/{env.bob_id}/permissions", json={"can_add_users": True})
    assert r.status_code == 200 and r.json()["user"]["can_add_users"] is True
    assert env.b.get("/api/me").json()["user"]["can_add_users"] is True
    # Bob 现在可以加人，新成员自动有与 Agent 的私聊
    ok = env.b.post("/api/users", json=new)
    assert ok.status_code == 200 and ok.json()["user"]["role"] == "user"
    dave = TestClientLogin(env, "dave", "password-dave")
    assert any(x["type"] == "agent_dm" for x in dave.get("/api/rooms").json()["rooms"])
    assert dave.post("/api/users", json={**new, "username": "erin"}).status_code == 403
    # 被授权者仍不能碰 Agent 凭证等 Owner 专属操作；Owner 可以收回
    assert env.b.post("/api/agent/credentials", json={"label": "x"}).status_code == 403
    env.a.patch(f"/api/users/{env.bob_id}/permissions", json={"can_add_users": False})
    assert env.b.post("/api/users", json={**new, "username": "erin"}).status_code == 403
    # 不能给 Owner / Agent 设置
    assert env.a.patch(f"/api/users/{env.alice_id}/permissions", json={"can_add_users": True}).status_code == 404
    assert env.a.patch(f"/api/users/{env.agent_id}/permissions", json={"can_add_users": True}).status_code == 404


def TestClientLogin(env, name, pw):
    from fastapi.testclient import TestClient

    c = TestClient(env.app)
    assert c.post("/api/login", json={"username": name, "password": pw}).status_code == 200
    return c
