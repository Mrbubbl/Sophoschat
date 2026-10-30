"""完整备份 / 恢复 / 一致性检查。

备份内容：数据库（消息、回顾、主题记忆、账号、凭证、游标、任务状态）+ 附件 + 签名密钥。
注意：备份包含 secret.key 与凭证哈希，请像保管密码一样保管备份文件。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from . import config, db

MANIFEST = "MANIFEST.json"


def create_backup(out_path: str | Path) -> dict:
    cfg = config.get()
    out = Path(out_path)
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "aichat.db"
        src = sqlite3.connect(str(cfg.db_path), timeout=30)
        dst = sqlite3.connect(str(snap))
        try:
            src.backup(dst)  # 在线一致性快照，服务运行时也安全
        finally:
            dst.close()
            src.close()
        chk = sqlite3.connect(str(snap))
        counts = {
            "messages": chk.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "memory_items": chk.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0],
            "attachments": chk.execute("SELECT COUNT(*) FROM attachments WHERE deleted_at IS NULL").fetchone()[0],
        }
        chk.close()
        manifest = {"format": 1, "created_at": db.now(), "counts": counts}
        (Path(td) / MANIFEST).write_text(json.dumps(manifest, indent=2))
        with tarfile.open(out, "w:gz") as tar:
            tar.add(snap, arcname="aichat.db")
            tar.add(Path(td) / MANIFEST, arcname=MANIFEST)
            if cfg.attach_dir.exists():
                tar.add(cfg.attach_dir, arcname="attachments")
            for extra in ("secret.key", "vapid_private.pem", "avatars"):
                f = cfg.data_dir / extra
                if f.exists():
                    tar.add(f, arcname=extra)
    return manifest


def restore_backup(archive: str | Path, data_dir: str | Path, force: bool = False) -> list[dict]:
    d = Path(data_dir).resolve()
    if d.exists() and any(d.iterdir()) and not force:
        raise SystemExit(f"目标目录 {d} 非空；确认要覆盖请加 --force")
    d.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if "aichat.db" not in names or MANIFEST not in names:
            raise SystemExit("不是有效的 aichat 备份文件")
        try:
            tar.extractall(d, filter="data")  # 拒绝绝对路径、.. 与特殊文件
        except TypeError:  # Python < 3.12
            for m in tar.getmembers():
                p = (d / m.name).resolve()
                if not str(p).startswith(str(d)) or m.issym() or m.islnk():
                    raise SystemExit(f"备份包含不安全路径：{m.name}")
            tar.extractall(d)
    for stale in ("aichat.db-wal", "aichat.db-shm"):
        (d / stale).unlink(missing_ok=True)
    config.load(d)
    db.connect(config.get().db_path)
    return post_restore_fixups()


def post_restore_fixups() -> list[dict]:
    """恢复后：把中断的任务放回队列、过期的互动任务标记过期，再做一致性检查。"""
    with db.tx():
        db.run("UPDATE agent_tasks SET status='queued', lease_owner=NULL, lease_credential_id=NULL, lease_until=NULL WHERE status='leased'")
        db.run("UPDATE agent_tasks SET status='expired', finished_at=?, error='expired' WHERE status='queued' AND kind='chat' AND expires_at<?", (db.now(), db.now()))
    return doctor()


def doctor() -> list[dict]:
    """返回问题列表；空列表表示一致。"""
    issues: list[dict] = []
    for r in db.all("SELECT r.id, r.max_seq, COALESCE(MAX(m.seq),0) AS real FROM rooms r LEFT JOIN messages m ON m.room_id=r.id GROUP BY r.id"):
        if r["max_seq"] != r["real"]:
            issues.append({"level": "error", "msg": f"房间 {r['id']} 的 max_seq={r['max_seq']} 与实际最大序号 {r['real']} 不一致"})
    for c in db.all("SELECT * FROM cursors"):
        last = db.one("SELECT COALESCE(MAX(id),0) AS m FROM events WHERE room_id=?", (c["room_id"],))["m"]
        if c["last_event_id"] > last:
            issues.append({"level": "error", "msg": f"房间 {c['room_id']} 的整理游标 {c['last_event_id']} 超过最新事件 {last}"})
    cfg = config.get()
    for a in db.all("SELECT id, path FROM attachments WHERE deleted_at IS NULL"):
        if not (cfg.data_dir / a["path"]).exists():
            issues.append({"level": "warn", "msg": f"附件 {a['id']} 的文件缺失"})
    for t in db.all("SELECT id FROM agent_tasks WHERE status='leased' AND lease_until<?", (db.now(),)):
        issues.append({"level": "info", "msg": f"任务 {t['id']} 租约已过期，将被调度器回收"})
    for i in db.all("SELECT i.id FROM memory_items i LEFT JOIN topics t ON t.id=i.topic_id WHERE i.topic_id IS NOT NULL AND t.id IS NULL"):
        issues.append({"level": "error", "msg": f"记忆条目 {i['id']} 指向不存在的主题"})
    return issues
