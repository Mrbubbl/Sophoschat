import types

import pytest
from fastapi.testclient import TestClient

from aichat import auth, calls, chat, config, db, hub
from aichat.main import create_app


class Env:
    def __init__(self, tmp_path):
        self.app = create_app(tmp_path / "data", scheduler=False)
        self.cfg = config.get()
        self.alice_id = auth.create_user("alice", "Alice", "password-alice", "owner")
        self.bob_id = auth.create_user("bob", "Bob", "password-bob", "user")
        self.carol_id = auth.create_user("carol", "Carol", "password-carol", "user")
        self.agent_id = auth.agent_user_id()
        for u in (self.alice_id, self.bob_id, self.carol_id):
            chat.bootstrap_user(u)
        self.a = TestClient(self.app)
        self.b = TestClient(self.app)
        self.c = TestClient(self.app)
        self.anon = TestClient(self.app)
        for cl, name, pw in ((self.a, "alice", "password-alice"), (self.b, "bob", "password-bob"), (self.c, "carol", "password-carol")):
            r = cl.post("/api/login", json={"username": name, "password": pw})
            assert r.status_code == 200, r.text
        # 两位主要使用者的私聊（含 Agent）
        self.dm = self.a.post("/api/rooms", json={"type": "dm", "user_id": self.bob_id}).json()["room"]["id"]
        self.agent_dm_a = chat.ensure_agent_dm(self.alice_id)
        self.agent_dm_b = chat.ensure_agent_dm(self.bob_id)
        self.cred_id, self.cred = auth.issue_credential("test")

    # ---- helpers
    def say(self, client, room_id, body, **kw):
        r = client.post(f"/api/rooms/{room_id}/messages", json={"body": body, **kw})
        assert r.status_code == 200, r.text
        return r.json()["message"]

    @property
    def cred_h(self):
        return {"Authorization": f"Bearer {self.cred}"}

    def lease(self, cred_h=None):
        r = self.anon.post("/api/agent/lease", headers=cred_h or self.cred_h)
        assert r.status_code == 200, r.text
        return r.json()["lease"]

    @staticmethod
    def th(lease):
        return {"Authorization": f"Bearer {lease['task_token']}"}

    def consolidate_lease(self, room_id, mode="pending"):
        """让某个 Room 立即产生整理任务并领取。"""
        config.get().consolidate_quiet = 0
        from aichat import tasks

        tasks.maintenance()
        # 把别的 Room 的整理任务跳过：只领取目标 Room 的
        while True:
            lease = self.lease()
            assert lease, "没有可领取的任务"
            if lease["task"]["room_id"] == room_id and lease["purpose"] == "consolidate":
                return lease
            self.anon.post(f"/api/agent/tasks/{lease['task']['id']}/fail", headers=self.cred_h, json={"error": "skip", "retry": False})


@pytest.fixture
def env(tmp_path):
    hub.reset()
    calls.reset()
    e = Env(tmp_path)
    yield e
    db.close()


def src(msg):
    return {"message_id": msg["id"], "version": msg["version"]}
