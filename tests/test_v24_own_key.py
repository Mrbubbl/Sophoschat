"""新成员默认不能用服务器 AI；可自己填 API（自己付费、不占服务器预算）；Owner 可开通。"""
from tests.test_v7_runtime import sdk, SDKEnv  # noqa: F401
from aichat import auth, chat, db, runtime


def _new_member(env):
    dave = auth.create_user("dave", "Dave", "password-dave", "user")
    chat.bootstrap_user(dave)
    from fastapi.testclient import TestClient
    c = TestClient(env.app)
    assert c.post("/api/login", json={"username": "dave", "password": "password-dave"}).status_code == 200
    return dave, c


def test_new_member_has_no_server_ai_until_own_key_or_owner_grants(env, sdk):
    dave, d = _new_member(env)
    assert runtime.credential_source(dave) is None
    assert d.get("/api/me").json()["user"]["agent_access"] is False
    room = chat.ensure_agent_dm(dave)
    n0 = db.one("SELECT COUNT(*) n FROM agent_tasks")["n"]
    env.say(d, room, "你好")
    assert db.one("SELECT COUNT(*) n FROM agent_tasks")["n"] == n0  # 没开通：不建任务

    # 自己填 API
    assert d.put("/api/me/agent-key", json={"base_url": "http://x.example", "model": "m", "api_key": "k"}).status_code == 422
    r = d.put("/api/me/agent-key", json={"base_url": "https://open.example.com/api/anthropic", "model": "glm-x", "api_key": "dave-own-secret"})
    assert r.status_code == 200 and r.json()["source"] == "own" and "dave-own-secret" not in r.text
    assert "dave-own-secret" not in d.get("/api/me/agent-key").text
    env.say(d, room, "现在可以了吗")
    lease = sdk.lease("chat")
    assert lease and lease["task"]["room_id"] == room
    cfg = env.anon.get(f"/api/agent/tasks/{lease['task']['id']}/runtime", headers=sdk.control(lease)).json()["config"]
    assert (cfg["model"], cfg["base_url"], cfg["api_key"]) == ("glm-x", "https://open.example.com/api/anthropic", "dave-own-secret")
    assert sdk.result(lease, 5.0).status_code == 200
    assert runtime._spent() < 5.0  # 自己的 Key 不占服务器预算

    # 删除自己的 Key 后回到无权限；Owner 开通后可用服务器 AI
    assert d.put("/api/me/agent-key", json={"base_url": "", "model": "", "api_key": ""}).json()["source"] is None
    assert env.b.patch(f"/api/users/{dave}/permissions", json={"agent_access": True}).status_code == 403
    assert env.a.patch(f"/api/users/{dave}/permissions", json={"agent_access": True}).status_code == 200
    assert runtime.credential_source(dave) == "server"
