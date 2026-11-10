"""安卓 App 后台连接：/ws?client=android 的连接会收到 native.notify，静音与发送者自己不收。"""
import asyncio

from aichat import db, hub, push


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)


def _run(coro):
    return asyncio.run(coro)


def test_native_connection_gets_notification_but_muted_and_sender_do_not(env):
    room = {"id": env.dm, "type": "dm", "title": ""}
    msg = {"id": 1, "body": "你好，看一下", "attachment": None}
    bob_native, alice_native, bob_web = FakeWS(), FakeWS(), FakeWS()

    async def go():
        await hub.connect(env.bob_id, bob_native, native=True)
        await hub.connect(env.bob_id, bob_web)  # 普通网页连接：不能收到 native.notify
        await hub.connect(env.alice_id, alice_native, native=True)
        await push.notify_message(room, env.alice_id, "Alice", [env.alice_id, env.bob_id], msg)

    _run(go())
    assert [m["type"] for m in bob_native.sent] == ["native.notify"]
    got = bob_native.sent[0]
    assert got["title"] == "Alice" and got["body"] == "你好，看一下" and got["room_id"] == env.dm and "badge" in got
    assert bob_web.sent == [] and alice_native.sent == []  # 网页连接不收；发送者自己不收

    db.run("UPDATE room_members SET muted=1 WHERE room_id=? AND user_id=?", (env.dm, env.bob_id))
    bob_native.sent.clear()
    _run(push.notify_message(room, env.alice_id, "Alice", [env.bob_id], msg))
    assert bob_native.sent == []  # 静音的聊天不弹通知
    hub.reset()
