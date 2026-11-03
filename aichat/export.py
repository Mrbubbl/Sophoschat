"""按 Room 与日期范围导出：Markdown / JSON，可附带主题记忆与附件（zip）。"""
from __future__ import annotations

import io
import json
import re
import zipfile

from . import chat, config, db, memory
from .errors import ApiError


def _sender_names(room_id: int) -> dict[int, str]:
    rows = db.all("SELECT DISTINCT u.id, u.display_name FROM users u JOIN room_members m ON m.user_id=u.id WHERE m.room_id=?", (room_id,))
    return {r["id"]: r["display_name"] for r in rows}


def _memory_dump(room_id: int) -> dict:
    topics = []
    for t in db.all("SELECT * FROM topics WHERE room_id=? ORDER BY id", (room_id,)):
        d = memory.ser_topic(t, with_counts=False)
        d["items"] = []
        for it in db.all("SELECT * FROM memory_items WHERE topic_id=? ORDER BY id", (t["id"],)):
            x = memory.ser_item(it)
            x["history"] = memory.item_history(it["id"], room_id)
            d["items"].append(x)
        topics.append(d)
    pending = []
    for it in db.all("SELECT * FROM memory_items WHERE room_id=? AND topic_id IS NULL ORDER BY id", (room_id,)):
        x = memory.ser_item(it)
        x["history"] = memory.item_history(it["id"], room_id)
        pending.append(x)
    return {"topics": topics, "unassigned_items": pending, "summaries": memory.list_summaries(room_id, 10000)}


def _collect(room_id: int, user_id: int, from_time: str | None, to_time: str | None) -> tuple[dict, list]:
    room = chat.require_member(room_id, user_id)
    sql = "SELECT * FROM messages WHERE room_id=? AND seq>? AND deleted_at IS NULL"
    args: list = [room_id, room["history_from_seq"]]
    if from_time:
        sql += " AND created_at>=?"
        args.append(from_time)
    if to_time:
        sql += " AND created_at<?"
        args.append(to_time)
    sql += " ORDER BY seq"
    rows = db.all(sql, args)
    return chat.ser_room(room, user_id), chat.ser_messages(rows, user_id)


def _md(room: dict, msgs: list, names: dict[int, str], memory_dump: dict | None) -> str:
    title = room["title"] or " / ".join(m["display_name"] for m in room["members"])
    out = [f"# {title}", "", f"导出时间：{db.now()}（所有时间为 UTC）", ""]
    day = None
    for m in msgs:
        d = m["created_at"][:10]
        if d != day:
            day = d
            out += [f"## {d}", ""]
        who = names.get(m["sender_id"], f"用户{m['sender_id']}") + (" 🤖" if m["kind"] == "agent" else "")
        edited = "（已编辑）" if m["edited_at"] else ""
        out.append(f"**{who}** · {m['created_at'][11:16]} · #{m['id']}{edited}")
        if m["reply_preview"]:
            rp = m["reply_preview"]
            out.append(f"> 回复 #{rp['id']}：{(rp['body'] or '[已删除]')[:60]}")
        if m["body"]:
            out.append("")
            out.append(m["body"])
        if m["attachment"]:
            out.append("")
            out.append(f"📎 {m['attachment']['filename']}（attachments/{m['attachment']['id']}_{m['attachment']['filename']}）")
        out.append("")
    if memory_dump:
        out += ["---", "", "# 主题记忆", ""]
        for t in memory_dump["topics"]:
            out += [f"## {t['title']}（{t['status']}）", "", t["overview"] or "", ""]
            for it in t["items"]:
                src = ", ".join(f"#{s['message_id']}v{s['version']}" for s in it["sources"]) or "手工"
                out.append(f"- [{it['state']}] {it['content']}  \n  来源：{src}" + (f" · 采纳：{it['adoption']}" if it["adoption"] else ""))
            out.append("")
        if memory_dump["summaries"]:
            out += ["## 回顾", ""]
            for s in memory_dump["summaries"]:
                out.append(f"- {s['period_start'] or ''} ~ {s['period_end'] or ''}：{s['content']}")
    return "\n".join(out) + "\n"


def export_room(room_id: int, user_id: int, fmt: str = "json", from_time: str | None = None, to_time: str | None = None, include_memory: bool = False, include_attachments: bool = False) -> tuple[bytes, str, str]:
    if fmt not in ("json", "md"):
        raise ApiError(400, "invalid_format", "format 必须是 json 或 md")
    room, msgs = _collect(room_id, user_id, from_time, to_time)
    names = _sender_names(room_id)
    mem = _memory_dump(room_id) if include_memory else None
    base = f"room-{room_id}"
    if fmt == "json":
        doc = {"exported_at": db.now(), "timezone": "UTC", "room": {k: room[k] for k in ("id", "type", "title", "members", "created_at")}, "messages": msgs}
        if mem:
            doc["memory"] = mem
        main = json.dumps(doc, ensure_ascii=False, indent=2).encode()
        main_name = f"{base}.json"
        mime = "application/json"
    else:
        main = _md(room, msgs, names, mem).encode()
        main_name = f"{base}.md"
        mime = "text/markdown; charset=utf-8"
    if not include_attachments:
        return main, main_name, mime
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(main_name, main)
        manifest = []
        for m in msgs:
            a = m["attachment"]
            if not a:
                continue
            row = db.one("SELECT * FROM attachments WHERE id=?", (a["id"],))
            p = config.get().data_dir / row["path"]
            if p.exists():
                safe = re.sub(r"[^\w.\-一-鿿]+", "_", a["filename"])
                arc = f"attachments/{a['id']}_{safe}"
                z.write(p, arc)
                manifest.append({"message_id": m["id"], "attachment_id": a["id"], "filename": a["filename"], "path": arc, "sha256": row["sha256"]})
        z.writestr("attachments.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue(), f"{base}.zip", "application/zip"
