"""v9：邀请链接、聊天列表置顶/标记未读、聊天背景与外观偏好。"""
from fastapi.testclient import TestClient

from aichat import db


def _close_registration(env):
    assert env.a.put("/api/registration", json={"enabled": False}).status_code == 200


def _group(env, title="项目讨论"):
    r = env.a.post("/api/rooms", json={"type": "group", "title": title, "member_ids": [env.bob_id]})
    assert r.status_code == 200, r.text
    return r.json()["room"]["id"]


def test_invite_lets_new_user_register_when_registration_closed(env):
    _close_registration(env)
    gid = _group(env)
    r = env.a.post("/api/invites", json={"hours": 24, "max_uses": 1, "room_id": gid, "note": "给小王"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    assert r.json()["invite"]["status"] == "active"
    # 数据库里只存哈希
    assert not db.one("SELECT 1 FROM invites WHERE token_hash=?", (token,))

    info = env.anon.get("/api/invite-info", params={"token": token}).json()
    assert info["valid"] and info["inviter"] == "Alice" and info["room_title"] == "项目讨论"

    # 没有邀请仍然注册不了
    assert env.anon.post("/api/register", json={"username": "nope", "password": "a-good-password"}).status_code == 403

    new = TestClient(env.app)
    r = new.post("/api/register", json={"username": "wang", "display_name": "小王", "password": "a-good-password", "invite": token})
    assert r.status_code == 200, r.text
    wid = r.json()["user"]["id"]
    rooms = {x["id"] for x in new.get("/api/rooms").json()["rooms"]}
    assert gid in rooms  # 直接进了群

    # 次数用完
    other = TestClient(env.app)
    r = other.post("/api/register", json={"username": "li", "password": "a-good-password", "invite": token})
    assert r.status_code == 403
    assert not db.one("SELECT 1 FROM users WHERE username='li'")  # 注册事务整体回滚
    assert env.anon.get("/api/invite-info", params={"token": token}).json() == {"valid": False, "reason": "used_up"}

    lst = env.a.get("/api/invites").json()["invites"]
    assert lst[0]["uses"] == 1 and lst[0]["used_by"][0]["id"] == wid


def test_invite_permissions_and_revoke(env):
    # 普通成员不能发邀请
    assert env.b.post("/api/invites", json={}).status_code == 403
    assert env.b.get("/api/invites").status_code == 403
    # 授权后可以，只能看到自己的
    assert env.a.patch(f"/api/users/{env.bob_id}/permissions", json={"can_add_users": True}).status_code == 200
    rb = env.b.post("/api/invites", json={"hours": 1, "max_uses": 2})
    assert rb.status_code == 200, rb.text
    ra = env.a.post("/api/invites", json={})
    assert len(env.b.get("/api/invites").json()["invites"]) == 1
    assert len(env.a.get("/api/invites").json()["invites"]) == 2
    # bob 不能撤销 alice 的
    assert env.b.delete(f"/api/invites/{ra.json()['invite']['id']}").status_code == 404
    iid = rb.json()["invite"]["id"]
    assert env.b.delete(f"/api/invites/{iid}").json()["invite"]["status"] == "revoked"
    assert env.anon.get("/api/invite-info", params={"token": rb.json()["token"]}).json()["reason"] == "revoked"
    # 参数校验
    assert env.a.post("/api/invites", json={"hours": 0.1}).status_code == 400
    assert env.a.post("/api/invites", json={"max_uses": 999}).status_code == 400
    assert env.a.post("/api/invites", json={"room_id": env.dm}).status_code == 400  # 私聊不能邀请
    assert env.c.post("/api/invites", json={}).status_code == 403


def test_invite_expired(env):
    r = env.a.post("/api/invites", json={"hours": 1}).json()
    db.run("UPDATE invites SET expires_at='2000-01-01T00:00:00+00:00'")
    assert env.anon.get("/api/invite-info", params={"token": r["token"]}).json()["reason"] == "expired"
    assert env.anon.post("/api/register", json={"username": "late", "password": "a-good-password", "invite": r["token"]}).status_code == 403
    assert env.anon.get("/api/invite-info", params={"token": "x" * 100}).json()["valid"] is False


def test_pin_and_mark_unread(env):
    r = env.a.post(f"/api/rooms/{env.dm}/pin", json={"value": True}).json()["room"]
    assert r["pinned"] is True
    r = env.a.post(f"/api/rooms/{env.dm}/unread", json={"value": True}).json()["room"]
    assert r["marked_unread"] is True
    # 只影响自己
    rb = [x for x in env.b.get("/api/rooms").json()["rooms"] if x["id"] == env.dm][0]
    assert rb["pinned"] is False and rb["marked_unread"] is False
    # 读一下就清除
    env.a.post(f"/api/rooms/{env.dm}/read", json={"seq": 0})
    ra = [x for x in env.a.get("/api/rooms").json()["rooms"] if x["id"] == env.dm][0]
    assert ra["marked_unread"] is False and ra["pinned"] is True


def test_room_background_and_prefs(env):
    assert env.a.put(f"/api/rooms/{env.dm}/background", json={"bg": "waves"}).json()["room"]["bg"] == "waves"
    assert env.a.put(f"/api/rooms/{env.dm}/background", json={"bg": "#112233"}).json()["room"]["bg"] == "#112233"
    assert env.a.put(f"/api/rooms/{env.dm}/background", json={"bg": "url(javascript:1)"}).status_code == 400
    assert env.a.put(f"/api/rooms/{env.dm}/background", json={"bg": None}).json()["room"]["bg"] is None

    me = env.a.get("/api/me").json()
    assert me["prefs"]["accent"] == "teal" and me["prefs"]["font"] == "m"
    p = env.a.put("/api/me/prefs", json={"accent": "violet", "font": "l", "bg": "dots", "voice_rate": 1.5, "autoplay": False}).json()["prefs"]
    assert p["accent"] == "violet" and p["font"] == "l" and p["bg"] == "dots" and p["voice_rate"] == 1.5 and p["autoplay"] is False
    assert env.a.put("/api/me/prefs", json={"accent": "red; x"}).status_code == 400
    assert env.a.put("/api/me/prefs", json={"motion": False}).json()["prefs"]["motion"] is False
    assert env.a.put("/api/me/prefs", json={"motion": 1}).status_code == 400
    assert env.a.put("/api/me/prefs", json={"evil": 1}).status_code == 400
    assert env.a.put("/api/me/prefs", json={"voice_rate": 3}).status_code == 400
    # bob 的不受影响
    assert env.b.get("/api/me").json()["prefs"]["accent"] == "teal"


def test_background_image_upload(env):
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    r = env.a.post("/api/me/background", files={"file": ("bg.png", png, "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["prefs"]["bg_image_url"]
    assert url and env.a.get(url).status_code == 200
    # 别人看不到（这个地址只返回当前登录用户自己的）
    assert env.b.get("/api/me/background").status_code == 404
    assert env.a.post("/api/me/background", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400
    assert env.a.delete("/api/me/background").json()["prefs"]["bg_image_url"] is None


def test_voice_waveform_saved_and_sanitised(env):
    webm = b"\x1a\x45\xdf\xa3" + b"\0" * 200
    r = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("v.webm", webm, "audio/webm")}, data={"duration_ms": "3200", "waveform": ",".join(str(i % 40) for i in range(80))})
    assert r.status_code == 200, r.text
    a = r.json()["attachment"]
    assert a["is_voice"] and len(a["waveform"]) == 64 and max(a["waveform"]) <= 31
    bad = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("v.webm", webm, "audio/webm")}, data={"duration_ms": "1000", "waveform": "x,y"}).json()["attachment"]
    assert bad["waveform"] is None
    # 普通文件不保存波形
    f = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("a.txt", b"hi", "text/plain")}, data={"waveform": "1,2,3,4,5,6,7,8"}).json()["attachment"]
    assert f["waveform"] is None
