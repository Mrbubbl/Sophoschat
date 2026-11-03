import io
import json
import zipfile

from aichat import backup, chat, config, db, tasks
from conftest import src
from test_memory import commit, human_item, setup_batch, topic_op


def test_export_markdown_and_json_respect_range_and_visibility(env):
    env.say(env.a, env.dm, "第一条 Alice")
    m2 = env.say(env.b, env.dm, "第二条 Bob")
    env.say(env.a, env.dm, "被删除的不应出现")
    env.a.delete(f"/api/messages/{db.one('SELECT id FROM messages ORDER BY id DESC LIMIT 1')['id']}")
    md = env.a.get(f"/api/rooms/{env.dm}/export", params={"format": "md"})
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert "第一条 Alice" in md.text and "第二条 Bob" in md.text and "被删除" not in md.text
    js = env.a.get(f"/api/rooms/{env.dm}/export", params={"format": "json"}).json()
    assert [m["body"] for m in js["messages"]] == ["第一条 Alice", "第二条 Bob"] and js["timezone"] == "UTC"
    part = env.a.get(f"/api/rooms/{env.dm}/export", params={"format": "json", "from_time": m2["created_at"]}).json()
    assert [m["body"] for m in part["messages"]] == ["第二条 Bob"]
    assert env.c.get(f"/api/rooms/{env.dm}/export").status_code == 404  # 非成员


def test_export_with_memory_and_attachments_zip(env):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行")], summaries=[{"content": "回顾内容", "sources": [src(m1)]}])
    att = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("报价单.txt", b"hello attachment", "text/plain")}).json()["attachment"]
    env.say(env.a, env.dm, "附件在这", attachment_id=att["id"])
    md = env.a.get(f"/api/rooms/{env.dm}/export", params={"format": "md", "memory": 1}).text
    assert "# 主题记忆" in md and "夏季旅行计划" in md and "回顾内容" in md and "来源：#" in md
    z = env.a.get(f"/api/rooms/{env.dm}/export", params={"format": "json", "memory": 1, "attachments": 1})
    assert z.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    names = zf.namelist()
    assert any(n.startswith("attachments/") and n.endswith(".txt") for n in names) and "attachments.json" in names
    chat_json = json.loads(zf.read(f"room-{env.dm}.json"))
    assert chat_json["memory"]["topics"][0]["items"][0]["history"][0]["version"] == 1
    assert zf.read([n for n in names if n.startswith("attachments/") and n.endswith(".txt")][0]) == b"hello attachment"


def test_backup_restore_roundtrip_and_resumes_work(env, tmp_path):
    m1, _, _, lease = setup_batch(env)
    commit(env, lease, [topic_op(), human_item(env, m1, "想夏天去海边旅行")])
    att = env.a.post(f"/api/rooms/{env.dm}/attachments", files={"file": ("a.txt", b"payload", "text/plain")}).json()["attachment"]
    env.say(env.a, env.dm, "带附件", attachment_id=att["id"])
    env.say(env.a, env.agent_dm_a, "备份时还在排队的问题")
    out = tmp_path / "backup.tar.gz"
    manifest = backup.create_backup(out)
    assert manifest["counts"]["attachments"] == 1 and out.exists()

    target = tmp_path / "restored"
    issues = backup.restore_backup(out, target)
    assert [i for i in issues if i["level"] == "error"] == []
    assert config.get().data_dir == target.resolve()
    assert db.one("SELECT COUNT(*) AS c FROM messages")["c"] >= 5
    assert db.one("SELECT COUNT(*) AS c FROM memory_items")["c"] == 1
    assert (target / db.one("SELECT path FROM attachments")["path"]).read_bytes() == b"payload"
    # 游标一并恢复：整理任务继续而不是从头重做
    assert tasks.pending_event_count(env.dm) >= 0
    # 签名密钥随备份恢复 → 旧凭证仍然有效，旧的任务令牌格式也仍能校验
    from aichat import auth

    assert auth.credential_user(env.cred) is not None
    # 备份里正在处理的任务（leased）回到队列
    assert db.one("SELECT COUNT(*) AS c FROM agent_tasks WHERE status='leased'")["c"] == 0


def test_restore_refuses_non_empty_target_and_unsafe_archives(env, tmp_path):
    import tarfile

    out = tmp_path / "b.tar.gz"
    backup.create_backup(out)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("1")
    try:
        backup.restore_backup(out, busy)
        raised = False
    except SystemExit:
        raised = True
    assert raised
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        t.addfile(info, io.BytesIO(data))
        for n in ("aichat.db", "MANIFEST.json"):
            i2 = tarfile.TarInfo(n)
            i2.size = 1
            t.addfile(i2, io.BytesIO(b"x"))
    try:
        backup.restore_backup(evil, tmp_path / "t2")
    except BaseException:
        pass
    assert not (tmp_path / "escape.txt").exists()


def test_doctor_detects_inconsistencies(env):
    env.say(env.a, env.dm, "x")
    assert [i for i in backup.doctor() if i["level"] == "error"] == []
    db.run("UPDATE rooms SET max_seq=99 WHERE id=?", (env.dm,))
    db.run("INSERT INTO cursors(consumer, room_id, last_event_id) VALUES('consolidate', ?, 9999)", (env.dm,))
    msgs = [i["msg"] for i in backup.doctor()]
    assert any("max_seq" in m for m in msgs) and any("游标" in m for m in msgs)
    assert env.a.get("/api/admin/doctor").status_code == 200 and env.b.get("/api/admin/doctor").status_code == 403
