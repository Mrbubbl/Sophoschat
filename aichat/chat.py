"""普通聊天：房间、成员、消息、历史、搜索、附件、收藏、已读。"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from . import auth, config, db, hub, memory, push, tasks
from .errors import ApiError

MAX_BODY = 20000
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_\-]{2,32})")
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
AUDIO_MIMES = {"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/aac", "audio/wav", "audio/x-m4a"}
MAX_REACTIONS_PER_MESSAGE = 12


# ---------------------------------------------------------------- 成员与权限
def member_rows(room_id: int):
    return db.all(
        "SELECT u.id, u.username, u.display_name, u.role, u.avatar, m.history_from_seq, m.joined_at "
        "FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.left_at IS NULL ORDER BY u.id",
        (room_id,),
    )


def member_ids(room_id: int) -> list[int]:
    return [r["id"] for r in member_rows(room_id)]


def human_member_ids(room_id: int) -> list[int]:
    return [r["id"] for r in member_rows(room_id) if r["role"] != "agent"]


def agent_member_id(room_id: int) -> int | None:
    r = db.one(
        "SELECT m.user_id FROM room_members m JOIN users u ON u.id=m.user_id "
        "WHERE m.room_id=? AND u.role='agent' AND m.left_at IS NULL AND u.disabled=0",
        (room_id,),
    )
    return r["user_id"] if r else None


def require_member(room_id: int, user_id: int):
    r = db.one(
        "SELECT r.*, m.last_read_seq, m.archived, m.muted, m.history_from_seq FROM rooms r "
        "JOIN room_members m ON m.room_id=r.id WHERE r.id=? AND m.user_id=? AND m.left_at IS NULL",
        (room_id, user_id),
    )
    if not r:
        raise ApiError(404, "room_not_found", "会话不存在或无权访问")
    return r


def _notify(room_id: int, payload: dict, only_humans: bool = True) -> None:
    ids = human_member_ids(room_id) if only_humans else member_ids(room_id)
    db.on_commit(lambda: hub.publish(ids, payload))


def add_event(room_id: int, type_: str, message_id: int | None = None, version: int | None = None, actor: int | None = None) -> int:
    return db.insert(
        "INSERT INTO events(room_id, type, message_id, message_version, actor_id, created_at) VALUES(?,?,?,?,?,?)",
        (room_id, type_, message_id, version, actor, db.now()),
    )


# ---------------------------------------------------------------- 序列化
def _preview(r) -> dict | None:
    if not r:
        return None
    deleted = r["deleted_at"] is not None
    body = None if deleted else (r["body"] or "")
    return {
        "id": r["id"],
        "seq": r["seq"],
        "sender_id": r["sender_id"],
        "kind": r["kind"],
        "body": body[:120] if body is not None else None,
        "deleted": deleted,
        "has_attachment": r["attachment_id"] is not None and not deleted,
        "created_at": r["created_at"],
    }


def ser_room(r, user_id: int) -> dict:
    rid = r["id"]
    hist = r["history_from_seq"]
    last = db.one("SELECT * FROM messages WHERE room_id=? AND seq>? ORDER BY seq DESC LIMIT 1", (rid, hist))
    unread = db.one(
        "SELECT COUNT(*) AS c FROM messages WHERE room_id=? AND seq>? AND seq>? AND sender_id!=? AND deleted_at IS NULL",
        (rid, r["last_read_seq"], hist, user_id),
    )["c"]
    members = [dict(id=m["id"], username=m["username"], display_name=m["display_name"], role=m["role"], avatar_url=auth.avatar_url(m)) for m in member_rows(rid)]
    agent = next((m for m in members if m["role"] == "agent"), None)
    return {
        "id": rid,
        "type": r["type"],
        "title": r["title"],
        "created_at": r["created_at"],
        "members": members,
        "agent_in_room": agent is not None,
        "max_seq": r["max_seq"],
        "last_read_seq": r["last_read_seq"],
        "unread": unread,
        "archived": bool(r["archived"]),
        "muted": bool(r["muted"]),
        "consolidation_enabled": bool(r["consolidation_enabled"]),
        "retention_days": r["retention_days"],
        "last_message": _preview(last),
        "last_activity": last["created_at"] if last else r["created_at"],
    }


def ser_attachment(a) -> dict | None:
    if not a or a["deleted_at"]:
        return None
    return {
        "id": a["id"],
        "filename": a["filename"],
        "mime": a["mime"],
        "size": a["size"],
        "is_image": a["mime"] in IMAGE_MIMES,
        "is_voice": a["mime"] in AUDIO_MIMES and a["duration_ms"] is not None,
        "duration_ms": a["duration_ms"],
        "transcript": a["transcript"],
        "url": f"/api/attachments/{a['id']}",
    }


def ser_messages(rows, viewer_id: int | None = None) -> list[dict]:
    rows = list(rows)
    if not rows:
        return []
    att_ids = [r["attachment_id"] for r in rows if r["attachment_id"]]
    reply_ids = [r["reply_to"] for r in rows if r["reply_to"]]
    atts, replies, starred = {}, {}, set()
    reacts: dict[int, dict[str, list[int]]] = {}
    qm = ",".join("?" * len(rows))
    for x in db.all(f"SELECT message_id, emoji, user_id FROM reactions WHERE message_id IN ({qm}) ORDER BY created_at", [r["id"] for r in rows]):
        reacts.setdefault(x["message_id"], {}).setdefault(x["emoji"], []).append(x["user_id"])
    if att_ids:
        q = ",".join("?" * len(att_ids))
        atts = {a["id"]: a for a in db.all(f"SELECT * FROM attachments WHERE id IN ({q})", att_ids)}
    if reply_ids:
        q = ",".join("?" * len(reply_ids))
        replies = {x["id"]: x for x in db.all(f"SELECT * FROM messages WHERE id IN ({q})", reply_ids)}
    if viewer_id:
        q = ",".join("?" * len(rows))
        starred = {s["message_id"] for s in db.all(f"SELECT message_id FROM stars WHERE user_id=? AND message_id IN ({q})", [viewer_id] + [r["id"] for r in rows])}
    out = []
    for r in rows:
        deleted = r["deleted_at"] is not None
        out.append(
            {
                "id": r["id"],
                "room_id": r["room_id"],
                "seq": r["seq"],
                "sender_id": r["sender_id"],
                "kind": r["kind"],
                "body": None if deleted else r["body"],
                "deleted": deleted,
                "reply_to": r["reply_to"],
                "reply_preview": _preview(replies.get(r["reply_to"])) if r["reply_to"] else None,
                "attachment": None if deleted else ser_attachment(atts.get(r["attachment_id"])),
                "mentions": db.jd(r["mentions"], []),
                "version": r["version"],
                "created_at": r["created_at"],
                "edited_at": r["edited_at"],
                "starred": r["id"] in starred,
                "reactions": [] if deleted else [{"emoji": e, "user_ids": u} for e, u in reacts.get(r["id"], {}).items()],
                "client_msg_id": r["client_msg_id"],
            }
        )
    return out


def ser_message(r, viewer_id: int | None = None) -> dict:
    return ser_messages([r], viewer_id)[0]


# ---------------------------------------------------------------- 房间管理
def _check_user(uid: int, humans_only=True):
    u = auth.get_user(uid)
    if not u or u["disabled"] or (humans_only and u["role"] == "agent"):
        raise ApiError(404, "user_not_found", "用户不存在")
    return u


def _add_member(room_id: int, user_id: int, history_from: int = 0) -> None:
    db.run(
        "INSERT INTO room_members(room_id, user_id, joined_at, history_from_seq) VALUES(?,?,?,?) "
        "ON CONFLICT(room_id, user_id) DO UPDATE SET left_at=NULL, history_from_seq=excluded.history_from_seq",
        (room_id, user_id, db.now(), history_from),
    )


def ensure_agent_dm(user_id: int) -> int:
    agent = auth.ensure_agent_user()
    key = f"agent:{user_id}"
    r = db.one("SELECT id FROM rooms WHERE dm_key=?", (key,))
    if r:
        return r["id"]
    with db.tx():
        rid = db.insert(
            "INSERT INTO rooms(type, title, created_by, created_at, dm_key) VALUES('agent_dm', NULL, ?, ?, ?)",
            (user_id, db.now(), key),
        )
        _add_member(rid, user_id)
        _add_member(rid, agent)
    return rid


def create_dm(creator_id: int, other_id: int, include_agent: bool = True) -> int:
    if creator_id == other_id:
        raise ApiError(400, "invalid_target", "不能和自己创建私聊")
    _check_user(other_id)
    key = f"dm:{min(creator_id, other_id)}:{max(creator_id, other_id)}"
    r = db.one("SELECT id FROM rooms WHERE dm_key=?", (key,))
    if r:
        return r["id"]
    with db.tx():
        rid = db.insert("INSERT INTO rooms(type, created_by, created_at, dm_key) VALUES('dm', ?, ?, ?)", (creator_id, db.now(), key))
        _add_member(rid, creator_id)
        _add_member(rid, other_id)
        agent = auth.agent_user_id()
        if include_agent and agent:
            _add_member(rid, agent)
        add_event(rid, "member.changed", actor=creator_id)
        db.audit(f"user:{creator_id}", "room.create", rid, type="dm")
    _publish_room_update(rid)
    return rid


def create_group(creator_id: int, title: str, member_ids_: list[int], include_agent: bool = True) -> int:
    title = (title or "").strip()[:60]
    if not title:
        raise ApiError(400, "title_required", "请填写群名称")
    ids = [creator_id] + [int(x) for x in member_ids_ if int(x) != creator_id]
    for uid in ids:
        _check_user(uid)
    with db.tx():
        rid = db.insert("INSERT INTO rooms(type, title, created_by, created_at) VALUES('group', ?, ?, ?)", (title, creator_id, db.now()))
        for uid in dict.fromkeys(ids):
            _add_member(rid, uid)
        agent = auth.agent_user_id()
        if include_agent and agent:
            _add_member(rid, agent)
        add_event(rid, "member.changed", actor=creator_id)
        db.audit(f"user:{creator_id}", "room.create", rid, type="group")
    _publish_room_update(rid)
    return rid


def _publish_room_update(room_id: int) -> None:
    def push():
        for uid in human_member_ids(room_id):
            r = db.one(
                "SELECT r.*, m.last_read_seq, m.archived, m.muted, m.history_from_seq FROM rooms r "
                "JOIN room_members m ON m.room_id=r.id WHERE r.id=? AND m.user_id=?",
                (room_id, uid),
            )
            if r:
                hub.publish([uid], {"type": "room.updated", "room": ser_room(r, uid)})

    db.on_commit(push)


def list_rooms(user_id: int) -> list[dict]:
    rows = db.all(
        "SELECT r.*, m.last_read_seq, m.archived, m.muted, m.history_from_seq FROM rooms r "
        "JOIN room_members m ON m.room_id=r.id WHERE m.user_id=? AND m.left_at IS NULL",
        (user_id,),
    )
    out = [ser_room(r, user_id) for r in rows]
    out.sort(key=lambda x: x["last_activity"], reverse=True)
    return out


def get_room(room_id: int, user_id: int) -> dict:
    return ser_room(require_member(room_id, user_id), user_id)


def update_room(room_id: int, user_id: int, changes: dict) -> dict:
    room = require_member(room_id, user_id)
    with db.tx():
        if "title" in changes:
            if room["type"] != "group":
                raise ApiError(400, "not_group", "只有群聊可以改名")
            t = (changes["title"] or "").strip()[:60]
            if not t:
                raise ApiError(400, "title_required", "群名称不能为空")
            db.run("UPDATE rooms SET title=? WHERE id=?", (t, room_id))
        if "consolidation_enabled" in changes:
            db.run("UPDATE rooms SET consolidation_enabled=? WHERE id=?", (1 if changes["consolidation_enabled"] else 0, room_id))
            db.audit(f"user:{user_id}", "room.consolidation", room_id, enabled=bool(changes["consolidation_enabled"]))
        if "retention_days" in changes:
            d = changes["retention_days"]
            if d is not None:
                d = int(d)
                if d < 1 or d > 36500:
                    raise ApiError(400, "invalid_retention", "保留天数不合法")
            db.run("UPDATE rooms SET retention_days=? WHERE id=?", (d, room_id))
            db.audit(f"user:{user_id}", "room.retention", room_id, days=d)
    _publish_room_update(room_id)
    return get_room(room_id, user_id)


def set_member_flag(room_id: int, user_id: int, flag: str, value: bool) -> dict:
    require_member(room_id, user_id)
    if flag not in ("archived", "muted"):
        raise ApiError(400, "invalid_flag")
    db.run(f"UPDATE room_members SET {flag}=? WHERE room_id=? AND user_id=?", (1 if value else 0, room_id, user_id))
    r = require_member(room_id, user_id)
    payload = {"type": "room.updated", "room": ser_room(r, user_id)}
    hub.publish([user_id], payload)
    return payload["room"]


def set_agent_in_room(room_id: int, user_id: int, enabled: bool, history: str = "from_now") -> dict:
    room = require_member(room_id, user_id)
    if room["type"] == "agent_dm":
        raise ApiError(400, "agent_dm_fixed", "Agent 私聊中 Agent 固定参与")
    agent = auth.agent_user_id()
    if not agent:
        raise ApiError(400, "no_agent", "尚未创建 Agent 账号")
    with db.tx():
        if enabled:
            hist = 0 if history == "all" else room["max_seq"]
            _add_member(room_id, agent, hist)
            if history != "all":
                # 新加入的 Agent 不应回头整理加入之前的聊天
                last_ev = db.one("SELECT COALESCE(MAX(id),0) AS m FROM events WHERE room_id=?", (room_id,))["m"]
                db.run(
                    "INSERT INTO cursors(consumer, room_id, last_event_id, updated_at) VALUES('consolidate',?,?,?) "
                    "ON CONFLICT(consumer, room_id) DO UPDATE SET last_event_id=MAX(last_event_id, excluded.last_event_id), updated_at=excluded.updated_at",
                    (room_id, last_ev, db.now()),
                )
        else:
            db.run("UPDATE room_members SET left_at=? WHERE room_id=? AND user_id=?", (db.now(), room_id, agent))
            tasks.cancel_room_tasks(room_id, "agent_removed")
        add_event(room_id, "member.changed", actor=user_id)
        db.audit(f"user:{user_id}", "room.agent", room_id, enabled=enabled, history=history)
    _publish_room_update(room_id)
    return get_room(room_id, user_id)


def add_member(room_id: int, actor_id: int, new_user_id: int, history: str = "from_now") -> dict:
    room = require_member(room_id, actor_id)
    if room["type"] != "group":
        raise ApiError(400, "not_group", "只有群聊可以添加成员")
    _check_user(new_user_id)
    with db.tx():
        _add_member(room_id, new_user_id, 0 if history == "all" else room["max_seq"])
        add_event(room_id, "member.changed", actor=actor_id)
        db.audit(f"user:{actor_id}", "room.member_add", room_id, user_id=new_user_id, history=history)
    _publish_room_update(room_id)
    return get_room(room_id, actor_id)


def remove_member(room_id: int, actor_id: int, user_id: int) -> None:
    room = require_member(room_id, actor_id)
    if room["type"] != "group":
        raise ApiError(400, "not_group", "只有群聊可以移除成员")
    if actor_id != user_id and room["created_by"] != actor_id:
        raise ApiError(403, "forbidden", "只有群创建者可以移除他人")
    ids_before = human_member_ids(room_id)
    with db.tx():
        db.run("UPDATE room_members SET left_at=? WHERE room_id=? AND user_id=?", (db.now(), room_id, user_id))
        add_event(room_id, "member.changed", actor=actor_id)
        db.audit(f"user:{actor_id}", "room.member_remove", room_id, user_id=user_id)
    removed = user_id
    db.on_commit(lambda: hub.publish([removed], {"type": "room.removed", "room_id": room_id}))
    _publish_room_update(room_id)
    del ids_before


# ---------------------------------------------------------------- 消息
def parse_mentions(room_id: int, body: str) -> list[int]:
    names = {n.lower() for n in MENTION_RE.findall(body or "")}
    if not names:
        return []
    return [m["id"] for m in member_rows(room_id) if m["username"].lower() in names]


def send_message(room_id: int, sender_id: int, body: str, client_msg_id: str | None = None, reply_to: int | None = None, attachment_id: int | None = None) -> tuple[dict, bool]:
    body = (body or "").strip()
    if not body and not attachment_id:
        raise ApiError(400, "empty_message", "消息不能为空")
    if len(body) > MAX_BODY:
        raise ApiError(400, "message_too_long", f"消息过长（上限 {MAX_BODY} 字符）")
    if client_msg_id is not None:
        client_msg_id = str(client_msg_id)[:64]
    with db.tx():
        room = require_member(room_id, sender_id)
        sender = auth.get_user(sender_id)
        if client_msg_id:
            dup = db.one("SELECT * FROM messages WHERE room_id=? AND sender_id=? AND client_msg_id=?", (room_id, sender_id, client_msg_id))
            if dup:
                return ser_message(dup, sender_id), True
        if reply_to:
            p = db.one("SELECT id FROM messages WHERE id=? AND room_id=?", (reply_to, room_id))
            if not p:
                raise ApiError(400, "invalid_reply", "被回复的消息不在本会话")
        if attachment_id:
            a = db.one("SELECT * FROM attachments WHERE id=? AND room_id=? AND uploader_id=? AND message_id IS NULL AND deleted_at IS NULL", (attachment_id, room_id, sender_id))
            if not a:
                raise ApiError(400, "invalid_attachment", "附件无效或已被使用")
        seq = room["max_seq"] + 1
        db.run("UPDATE rooms SET max_seq=? WHERE id=?", (seq, room_id))
        mentions = parse_mentions(room_id, body)
        kind = "agent" if sender["role"] == "agent" else "human"
        mid = db.insert(
            "INSERT INTO messages(room_id, seq, sender_id, kind, body, reply_to, attachment_id, client_msg_id, mentions, version, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (room_id, seq, sender_id, kind, body, reply_to, attachment_id, client_msg_id, db.jl(mentions), db.now()),
        )
        db.run("INSERT INTO message_versions(message_id, version, body, created_at) VALUES(?,1,?,?)", (mid, body, db.now()))
        if attachment_id:
            db.run("UPDATE attachments SET message_id=? WHERE id=?", (mid, attachment_id))
        db.run("UPDATE room_members SET last_read_seq=MAX(last_read_seq, ?) WHERE room_id=? AND user_id=?", (seq, room_id, sender_id))
        add_event(room_id, "message.created", mid, 1, sender_id)
        msg = db.one("SELECT * FROM messages WHERE id=?", (mid,))
        if kind == "human":
            tasks.maybe_create_chat_task(room, msg, mentions)
        data = ser_message(msg)
        _notify(room_id, {"type": "message.created", "message": data})
        _push_later(room, sender, data)
    return ser_message(msg, sender_id), False


def _push_later(room, sender, data: dict) -> None:
    rid = room["id"]
    recipients = human_member_ids(rid)
    room_info = {"id": rid, "type": room["type"], "title": room["title"]}

    def go():
        import asyncio

        try:
            asyncio.get_running_loop().create_task(push.notify_message(room_info, sender["id"], sender["display_name"], recipients, data))
        except RuntimeError:
            pass

    db.on_commit(go)


def edit_message(message_id: int, user_id: int, body: str) -> dict:
    body = (body or "").strip()
    if not body:
        raise ApiError(400, "empty_message", "消息不能为空")
    if len(body) > MAX_BODY:
        raise ApiError(400, "message_too_long", "消息过长")
    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not m:
            raise ApiError(404, "message_not_found")
        require_member(m["room_id"], user_id)
        if m["sender_id"] != user_id:
            raise ApiError(403, "forbidden", "只能编辑自己的消息")
        if m["deleted_at"]:
            raise ApiError(409, "message_deleted", "消息已删除")
        if body == m["body"]:
            return ser_message(m, user_id)
        ver = m["version"] + 1
        db.run("UPDATE messages SET body=?, version=?, edited_at=? WHERE id=?", (body, ver, db.now(), message_id))
        db.run("INSERT INTO message_versions(message_id, version, body, created_at) VALUES(?,?,?,?)", (message_id, ver, body, db.now()))
        add_event(m["room_id"], "message.edited", message_id, ver, user_id)
        memory.on_message_edited(m["room_id"], message_id, ver)
        new = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        _notify(m["room_id"], {"type": "message.updated", "message": ser_message(new)})
    return ser_message(new, user_id)


def purge_message(m, actor: str, actor_id: int | None = None) -> None:
    """真正清除正文（不是只改状态位）：消息体、历史版本、附件文件、收藏、派生记忆。"""
    mid = m["id"]
    db.run("UPDATE messages SET body=NULL, deleted_at=?, attachment_id=attachment_id WHERE id=?", (db.now(), mid))
    db.run("UPDATE message_versions SET body=NULL WHERE message_id=?", (mid,))
    db.run("DELETE FROM stars WHERE message_id=?", (mid,))
    db.run("DELETE FROM reactions WHERE message_id=?", (mid,))
    for a in db.all("SELECT * FROM attachments WHERE message_id=? AND deleted_at IS NULL", (mid,)):
        db.run("UPDATE attachments SET deleted_at=? WHERE id=?", (db.now(), a["id"]))
        p = config.get().data_dir / a["path"]
        db.on_commit(lambda p=p: p.unlink(missing_ok=True))
    add_event(m["room_id"], "message.deleted", mid, m["version"], actor_id)
    memory.on_message_deleted(m["room_id"], mid)
    tasks.cancel_for_message(mid, "source_deleted")
    db.audit(actor, "message.delete", m["room_id"], message_id=mid)


def delete_message(message_id: int, user_id: int) -> None:
    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not m:
            raise ApiError(404, "message_not_found")
        require_member(m["room_id"], user_id)
        if m["sender_id"] != user_id:
            raise ApiError(403, "forbidden", "只能删除自己的消息")
        if m["deleted_at"]:
            return
        purge_message(m, f"user:{user_id}", user_id)
        new = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        _notify(m["room_id"], {"type": "message.deleted", "message": ser_message(new)})


def mark_read(room_id: int, user_id: int, seq: int) -> dict:
    room = require_member(room_id, user_id)
    seq = max(0, min(int(seq), room["max_seq"]))
    db.run("UPDATE room_members SET last_read_seq=MAX(last_read_seq, ?) WHERE room_id=? AND user_id=?", (seq, room_id, user_id))
    r = require_member(room_id, user_id)
    hub.publish([user_id], {"type": "read.updated", "room_id": room_id, "last_read_seq": r["last_read_seq"], "unread": ser_room(r, user_id)["unread"]})
    return {"last_read_seq": r["last_read_seq"]}


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def get_messages(room_id: int, user_id: int, *, before_seq=None, after_seq=None, around_seq=None, from_time=None, limit=50, q=None, sender_id=None) -> dict:
    room = require_member(room_id, user_id)
    hist = room["history_from_seq"]
    limit = max(1, min(int(limit or 50), 200))
    args: list = [room_id, hist]
    if q:
        sql = "SELECT * FROM messages WHERE room_id=? AND seq>? AND deleted_at IS NULL AND body LIKE ? ESCAPE '\\'"
        args.append(_like(q))
        if sender_id:
            sql += " AND sender_id=?"
            args.append(int(sender_id))
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        rows = db.all(sql, args)
        return {"messages": ser_messages(rows, user_id), "has_more_before": False, "has_more_after": False, "search": True}
    if from_time:
        first = db.one("SELECT seq FROM messages WHERE room_id=? AND seq>? AND created_at>=? ORDER BY seq LIMIT 1", (room_id, hist, from_time))
        if first:
            after_seq = first["seq"] - 1
        else:
            before_seq = None
            after_seq = None
    if around_seq is not None:
        half = max(1, limit // 2)
        before = db.all("SELECT * FROM messages WHERE room_id=? AND seq>? AND seq<=? ORDER BY seq DESC LIMIT ?", (room_id, hist, int(around_seq), half + 1))
        after = db.all("SELECT * FROM messages WHERE room_id=? AND seq>? AND seq>? ORDER BY seq ASC LIMIT ?", (room_id, hist, int(around_seq), half))
        rows = list(reversed(before)) + list(after)
    elif after_seq is not None:
        rows = db.all("SELECT * FROM messages WHERE room_id=? AND seq>? AND seq>? ORDER BY seq ASC LIMIT ?", (room_id, hist, int(after_seq), limit))
    else:
        if before_seq is not None:
            rows = db.all("SELECT * FROM messages WHERE room_id=? AND seq>? AND seq<? ORDER BY seq DESC LIMIT ?", (room_id, hist, int(before_seq), limit))
        else:
            rows = db.all("SELECT * FROM messages WHERE room_id=? AND seq>? ORDER BY seq DESC LIMIT ?", (room_id, hist, limit))
        rows = list(reversed(rows))
    more_before = more_after = False
    if rows:
        lo, hi = rows[0]["seq"], rows[-1]["seq"]
        more_before = bool(db.one("SELECT 1 FROM messages WHERE room_id=? AND seq>? AND seq<? LIMIT 1", (room_id, hist, lo)))
        more_after = bool(db.one("SELECT 1 FROM messages WHERE room_id=? AND seq>? LIMIT 1", (room_id, hi)))
    return {"messages": ser_messages(rows, user_id), "has_more_before": more_before, "has_more_after": more_after, "search": False}


def search_messages(user_id: int, q: str, room_id: int | None = None, sender_id: int | None = None, from_time: str | None = None, to_time: str | None = None, limit: int = 50) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    sql = (
        "SELECT m.* FROM messages m JOIN room_members rm ON rm.room_id=m.room_id AND rm.user_id=? AND rm.left_at IS NULL AND m.seq>rm.history_from_seq "
        "WHERE m.deleted_at IS NULL AND m.body LIKE ? ESCAPE '\\'"
    )
    args: list = [user_id, _like(q)]
    if room_id:
        sql += " AND m.room_id=?"
        args.append(int(room_id))
    if sender_id:
        sql += " AND m.sender_id=?"
        args.append(int(sender_id))
    if from_time:
        sql += " AND m.created_at>=?"
        args.append(from_time)
    if to_time:
        sql += " AND m.created_at<?"
        args.append(to_time)
    sql += " ORDER BY m.created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 100)))
    return ser_messages(db.all(sql, args), user_id)


def locate_message(message_id: int, user_id: int) -> dict:
    m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
    if not m:
        raise ApiError(404, "message_not_found")
    room = require_member(m["room_id"], user_id)
    if m["seq"] <= room["history_from_seq"]:
        raise ApiError(404, "message_not_found")
    return {"room_id": m["room_id"], "seq": m["seq"], "version": m["version"], "deleted": m["deleted_at"] is not None}


def set_star(user_id: int, message_id: int, starred: bool) -> None:
    m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
    if not m:
        raise ApiError(404, "message_not_found")
    require_member(m["room_id"], user_id)
    if starred and not m["deleted_at"]:
        db.run("INSERT OR IGNORE INTO stars(user_id, message_id, created_at) VALUES(?,?,?)", (user_id, message_id, db.now()))
    else:
        db.run("DELETE FROM stars WHERE user_id=? AND message_id=?", (user_id, message_id))


def list_stars(user_id: int, room_id: int) -> list[dict]:
    room = require_member(room_id, user_id)
    rows = db.all(
        "SELECT m.* FROM stars s JOIN messages m ON m.id=s.message_id WHERE s.user_id=? AND m.room_id=? AND m.seq>? AND m.deleted_at IS NULL ORDER BY m.seq DESC",
        (user_id, room_id, room["history_from_seq"]),
    )
    return ser_messages(rows, user_id)


def unread_total(user_id: int) -> int:
    """应用图标角标：未静音、未归档会话的未读消息总数。"""
    r = db.one(
        "SELECT COALESCE(SUM(MAX(r.max_seq - m.last_read_seq, 0)), 0) n FROM room_members m JOIN rooms r ON r.id=m.room_id "
        "WHERE m.user_id=? AND m.left_at IS NULL AND m.muted=0 AND m.archived=0",
        (user_id,),
    )
    return int(r["n"])


def set_transcript(att_id: int, room_id: int, text: str) -> dict:
    """Agent 写回语音转写：只能写本 Room 里、已发出的语音附件。"""
    text = (text or "").strip()[:20000]
    with db.tx():
        a = db.one("SELECT * FROM attachments WHERE id=? AND room_id=? AND deleted_at IS NULL AND message_id IS NOT NULL", (att_id, room_id))
        if not a or a["mime"] not in AUDIO_MIMES or a["duration_ms"] is None:
            raise ApiError(404, "attachment_not_found")
        db.run("UPDATE attachments SET transcript=? WHERE id=?", (text or None, att_id))
        m = db.one("SELECT * FROM messages WHERE id=?", (a["message_id"],))
        if m["deleted_at"]:
            raise ApiError(404, "attachment_not_found")
        _notify(room_id, {"type": "message.updated", "message": ser_message(m)})
    return {"ok": True}


# ---------------------------------------------------------------- 表情回应 / 正在输入
def set_reaction(message_id: int, user_id: int, emoji: str, on: bool) -> dict:
    emoji = (emoji or "").strip()
    if not emoji or len(emoji) > 16 or any(ch.isspace() or ord(ch) < 0x80 and not ch.isdigit() and ch not in "#*" for ch in emoji):
        raise ApiError(400, "invalid_emoji", "无效的表情")
    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not m:
            raise ApiError(404, "message_not_found")
        room = require_member(m["room_id"], user_id)
        if m["deleted_at"] or m["seq"] <= room["history_from_seq"]:
            raise ApiError(404, "message_not_found")
        if on:
            kinds = {r["emoji"] for r in db.all("SELECT DISTINCT emoji FROM reactions WHERE message_id=?", (message_id,))}
            if emoji not in kinds and len(kinds) >= MAX_REACTIONS_PER_MESSAGE:
                raise ApiError(400, "too_many_reactions", "这条消息的表情已经够多了")
            db.run("INSERT OR IGNORE INTO reactions(message_id, user_id, emoji, created_at) VALUES(?,?,?,?)", (message_id, user_id, emoji, db.now()))
        else:
            db.run("DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?", (message_id, user_id, emoji))
        data = ser_message(m, user_id)
        _notify(m["room_id"], {"type": "reaction.updated", "room_id": m["room_id"], "message_id": message_id, "reactions": data["reactions"]})
    return {"message_id": message_id, "reactions": data["reactions"]}


_typing_last: dict[tuple[int, int], float] = {}


def typing(room_id: int, user_id: int) -> None:
    """转发“正在输入”给同房间其他人。同一人同一房间 2 秒内最多转发一次；不落库。"""
    import time

    now = time.time()
    if now - _typing_last.get((room_id, user_id), 0) < 2:
        return
    try:
        require_member(room_id, user_id)
    except ApiError:
        return
    _typing_last[(room_id, user_id)] = now
    if len(_typing_last) > 2000:
        for k in [k for k, t in _typing_last.items() if now - t > 60]:
            _typing_last.pop(k, None)
    hub.publish([i for i in human_member_ids(room_id) if i != user_id], {"type": "typing", "room_id": room_id, "user_id": user_id})


# ---------------------------------------------------------------- 附件
def safe_filename(name: str) -> str:
    name = (name or "file").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".") or "file"
    return name[:120]


def new_attachment_path(room_id: int) -> tuple[Path, str]:
    rel = f"attachments/{room_id}/{uuid.uuid4().hex}"
    p = config.get().data_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p, rel


def register_attachment(room_id: int, uploader_id: int, filename: str, mime: str, size: int, rel_path: str, sha256: str, duration_ms: int | None = None) -> dict:
    require_member(room_id, uploader_id)
    aid = db.insert(
        "INSERT INTO attachments(room_id, uploader_id, filename, mime, size, path, sha256, created_at, duration_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        (room_id, uploader_id, safe_filename(filename), (mime or "application/octet-stream")[:100], size, rel_path, sha256, db.now(), duration_ms),
    )
    return ser_attachment(db.one("SELECT * FROM attachments WHERE id=?", (aid,)))


def get_attachment(att_id: int, user_id: int):
    a = db.one("SELECT * FROM attachments WHERE id=?", (att_id,))
    if not a or a["deleted_at"]:
        raise ApiError(404, "attachment_not_found")
    room = require_member(a["room_id"], user_id)
    if a["message_id"]:
        m = db.one("SELECT seq FROM messages WHERE id=?", (a["message_id"],))
        if m and m["seq"] <= room["history_from_seq"]:
            raise ApiError(404, "attachment_not_found")
    elif a["uploader_id"] != user_id:
        raise ApiError(404, "attachment_not_found")
    return a


# ---------------------------------------------------------------- 定时清理
def retention_sweep() -> int:
    n = 0
    for room in db.all("SELECT id, retention_days FROM rooms WHERE retention_days IS NOT NULL"):
        cutoff = db.ago(room["retention_days"] * 86400)
        rows = db.all("SELECT * FROM messages WHERE room_id=? AND deleted_at IS NULL AND created_at<?", (room["id"], cutoff))
        for m in rows:
            with db.tx():
                purge_message(m, "system:retention")
                new = db.one("SELECT * FROM messages WHERE id=?", (m["id"],))
                _notify(m["room_id"], {"type": "message.deleted", "message": ser_message(new)})
            n += 1
    return n


def orphan_attachment_sweep() -> int:
    n = 0
    for a in db.all("SELECT * FROM attachments WHERE message_id IS NULL AND deleted_at IS NULL AND created_at<?", (db.ago(86400),)):
        db.run("UPDATE attachments SET deleted_at=? WHERE id=?", (db.now(), a["id"]))
        (config.get().data_dir / a["path"]).unlink(missing_ok=True)
        n += 1
    return n


def bootstrap_user(new_user_id: int) -> None:
    """新用户创建后：建立与 Agent 的私聊，并与已有的每位用户建立私聊（默认包含 Agent，可在会话设置里关闭）。"""
    ensure_agent_dm(new_user_id)
    for r in db.all("SELECT id FROM users WHERE role!='agent' AND disabled=0 AND id!=?", (new_user_id,)):
        create_dm(new_user_id, r["id"], include_agent=True)
