import pytest

from aichat import db


def test_login_and_auth(env):
    assert env.anon.get("/api/rooms").status_code == 401
    r = env.anon.post("/api/login", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "bad_credentials"
    # agent 账号没有密码，不能登录
    r = env.anon.post("/api/login", json={"username": "agent", "password": ""})
    assert r.status_code == 401


def test_login_rate_limit(env):
    for _ in range(5):
        env.anon.post("/api/login", json={"username": "bob", "password": "nope"})
    r = env.anon.post("/api/login", json={"username": "bob", "password": "password-bob"})
    assert r.status_code == 429


def test_bad_origin_rejected(env):
    r = env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"
    ok = env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "x"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_send_idempotent_and_unread(env):
    m1 = env.say(env.a, env.dm, "你好 Bob", client_msg_id="k1")
    r = env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "你好 Bob", "client_msg_id": "k1"})
    assert r.json()["duplicate"] is True and r.json()["message"]["id"] == m1["id"]
    assert m1["seq"] == 1
    rooms = {x["id"]: x for x in env.b.get("/api/rooms").json()["rooms"]}
    assert rooms[env.dm]["unread"] == 1
    env.b.post(f"/api/rooms/{env.dm}/read", json={"seq": 1})
    rooms = {x["id"]: x for x in env.b.get("/api/rooms").json()["rooms"]}
    assert rooms[env.dm]["unread"] == 0
    # 自己发的消息不算未读
    assert {x["id"]: x for x in env.a.get("/api/rooms").json()["rooms"]}[env.dm]["unread"] == 0


def test_empty_and_too_long(env):
    assert env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "   "}).status_code == 400
    assert env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "x" * 20001}).status_code == 400


def test_non_member_cannot_read_or_write(env):
    # carol 与 alice 的私聊对 bob 不可见
    rid = env.a.post("/api/rooms", json={"type": "dm", "user_id": env.carol_id}).json()["room"]["id"]
    env.say(env.a, rid, "秘密")
    assert env.b.get(f"/api/rooms/{rid}/messages").status_code == 404
    assert env.b.post(f"/api/rooms/{rid}/messages", json={"body": "x"}).status_code == 404
    assert env.b.get(f"/api/rooms/{rid}").status_code == 404
    assert env.b.get("/api/search", params={"q": "秘密"}).json()["messages"] == []


def test_edit_and_delete_purge_body(env):
    m = env.say(env.a, env.dm, "原始内容")
    r = env.a.patch(f"/api/messages/{m['id']}", json={"body": "改后内容"})
    assert r.json()["message"]["version"] == 2 and r.json()["message"]["edited_at"]
    assert env.b.patch(f"/api/messages/{m['id']}", json={"body": "x"}).status_code == 403
    assert env.a.delete(f"/api/messages/{m['id']}").status_code == 200
    row = db.one("SELECT * FROM messages WHERE id=?", (m["id"],))
    assert row["body"] is None and row["deleted_at"]
    assert all(v["body"] is None for v in db.all("SELECT body FROM message_versions WHERE message_id=?", (m["id"],)))
    msgs = env.b.get(f"/api/rooms/{env.dm}/messages").json()["messages"]
    assert msgs[0]["deleted"] is True and msgs[0]["body"] is None
    # 搜索不再命中
    assert env.a.get("/api/search", params={"q": "内容"}).json()["messages"] == []
    # 已删除不可再编辑
    assert env.a.patch(f"/api/messages/{m['id']}", json={"body": "again"}).status_code == 409


def test_history_pagination_search_and_date_jump(env):
    for i in range(1, 61):
        env.say(env.a, env.dm, f"消息 {i}" + (" 苹果" if i % 20 == 0 else ""))
    page = env.b.get(f"/api/rooms/{env.dm}/messages", params={"limit": 25}).json()
    assert [m["seq"] for m in page["messages"]] == list(range(36, 61)) and page["has_more_before"]
    older = env.b.get(f"/api/rooms/{env.dm}/messages", params={"before_seq": 36, "limit": 25}).json()
    assert older["messages"][-1]["seq"] == 35 and older["messages"][0]["seq"] == 11
    around = env.b.get(f"/api/rooms/{env.dm}/messages", params={"around_seq": 30, "limit": 10}).json()
    seqs = [m["seq"] for m in around["messages"]]
    assert 30 in seqs and seqs == sorted(seqs) and around["has_more_before"] and around["has_more_after"]
    found = env.b.get("/api/search", params={"q": "苹果"}).json()["messages"]
    assert sorted(m["seq"] for m in found) == [20, 40, 60]
    # 通配符被当作普通字符
    assert env.b.get("/api/search", params={"q": "%"}).json()["messages"] == []
    # 按日期跳转：今天之前没有消息 → 从最早一条开始
    jump = env.b.get(f"/api/rooms/{env.dm}/messages", params={"from_time": "2000-01-01T00:00:00Z", "limit": 5}).json()
    assert jump["messages"][0]["seq"] == 1


def test_attachments_upload_download_and_purge(env, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    r = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("pic.png", png, "image/png")})
    assert r.status_code == 200
    att = r.json()["attachment"]
    assert att["is_image"] and att["size"] == len(png)
    m = env.say(env.a, env.dm, "", attachment_id=att["id"])
    assert m["attachment"]["id"] == att["id"]
    d = env.b.get(att["url"])
    assert d.status_code == 200 and d.content == png and d.headers["content-disposition"].startswith("inline")
    assert d.headers["x-content-type-options"] == "nosniff"
    # 非成员读不到
    other = env.a.post("/api/rooms", json={"type": "dm", "user_id": env.carol_id}).json()["room"]["id"]
    assert env.b.get(att["url"]).status_code == 200
    r2 = env.a.post(f"/api/rooms/{other}/attachments", files={"file": ("x.txt", b"hello", "text/plain")})
    assert env.b.get(r2.json()["attachment"]["url"]).status_code == 404
    # 伪装成图片的文件按普通附件处理，并强制下载
    fake = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("a.png", b"<script>alert(1)</script>", "image/png")}).json()["attachment"]
    assert fake["is_image"] is False
    env.say(env.a, env.dm, "", attachment_id=fake["id"])
    assert env.b.get(fake["url"]).headers["content-disposition"].startswith("attachment")
    # 一个附件不能被两条消息复用
    assert env.a.post(f"/api/rooms/{env.dm}/messages", json={"body": "again", "attachment_id": att["id"]}).status_code == 400
    # 删除消息后文件被清除
    path = env.cfg.data_dir / db.one("SELECT path FROM attachments WHERE id=?", (att["id"],))["path"]
    assert path.exists()
    env.a.delete(f"/api/messages/{m['id']}")
    assert not path.exists() and env.b.get(att["url"]).status_code == 404


def test_upload_size_limit(env):
    env.cfg.max_upload_mb = 1
    r = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("big.bin", b"0" * (1024 * 1024 + 10), "application/octet-stream")})
    assert r.status_code == 413


def test_archive_mute_star_stay_personal(env):
    m = env.say(env.a, env.dm, "收藏我")
    env.b.post(f"/api/rooms/{env.dm}/archive", json={"value": True})
    ra = {x["id"]: x for x in env.a.get("/api/rooms").json()["rooms"]}[env.dm]
    rb = {x["id"]: x for x in env.b.get("/api/rooms").json()["rooms"]}[env.dm]
    assert rb["archived"] and not ra["archived"]
    env.b.post(f"/api/messages/{m['id']}/star", json={"value": True})
    assert [x["id"] for x in env.b.get(f"/api/rooms/{env.dm}/stars").json()["messages"]] == [m["id"]]
    assert env.a.get(f"/api/rooms/{env.dm}/stars").json()["messages"] == []
    # 归档后仍可搜索
    assert env.b.get("/api/search", params={"q": "收藏"}).json()["messages"]


def test_group_history_visibility_for_new_member(env):
    rid = env.a.post("/api/rooms", json={"type": "group", "title": "三人群", "member_ids": [env.bob_id], "include_agent": False}).json()["room"]["id"]
    env.say(env.a, rid, "加入前的消息")
    env.a.post(f"/api/rooms/{rid}/members", json={"user_id": env.carol_id, "history": "from_now"})
    env.say(env.a, rid, "加入后的消息")
    msgs = env.c.get(f"/api/rooms/{rid}/messages").json()["messages"]
    assert [m["body"] for m in msgs] == ["加入后的消息"]
    assert env.c.get("/api/search", params={"q": "加入前"}).json()["messages"] == []


def test_websocket_pushes_new_message(env):
    # 同一个 TestClient 上下文共享一个事件循环（与真实服务单循环一致）；跨客户端的实时推送在集成测试里用真实服务验证
    with env.b:
        with env.b.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["user"]["username"] == "bob"
            env.say(env.b, env.dm, "实时消息")
            evt = ws.receive_json()
            assert evt["type"] == "message.created" and evt["message"]["body"] == "实时消息"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_websocket_requires_login_and_origin(env):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with env.anon.websocket_connect("/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with env.b.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass


def test_retention_sweep_purges_old_messages(env):
    from aichat import chat

    env.say(env.a, env.dm, "很旧的消息")
    db.run("UPDATE messages SET created_at='2000-01-01T00:00:00.000Z'")
    env.a.patch(f"/api/rooms/{env.dm}", json={"retention_days": 30})
    assert chat.retention_sweep() == 1
    assert db.one("SELECT body FROM messages WHERE room_id=?", (env.dm,))["body"] is None
