"""邀请链接 / 二维码：Owner 或有"可添加成员"权限的人生成，可设有效期、次数，可选直接进群。

只保存 token 的哈希；链接只在创建时返回一次。即使关闭了公开注册，持有效邀请的人仍可注册。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from . import auth, chat, db
from .errors import ApiError

MAX_HOURS = 24 * 30
MAX_USES = 50


def _hash(token: str) -> str:
    return hashlib.sha256(("invite:" + token).encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _status(r) -> str:
    if r["revoked_at"]:
        return "revoked"
    if r["uses"] >= r["max_uses"]:
        return "used_up"
    if _parse(r["expires_at"]) <= _now():
        return "expired"
    return "active"


def _can_manage(u) -> bool:
    return u["role"] == "owner" or bool(u["can_add_users"])


def ser(r, viewer_id: int | None = None) -> dict:
    creator = auth.get_user(r["created_by"])
    room = db.one("SELECT id, title, type FROM rooms WHERE id=?", (r["room_id"],)) if r["room_id"] else None
    used_by = [
        {"id": x["id"], "display_name": x["display_name"], "username": x["username"]}
        for x in db.all("SELECT u.id, u.display_name, u.username FROM invite_uses i JOIN users u ON u.id=i.user_id WHERE i.invite_id=? ORDER BY i.created_at", (r["id"],))
    ]
    return {
        "id": r["id"],
        "note": r["note"] or "",
        "room": {"id": room["id"], "title": room["title"] or "群聊"} if room else None,
        "max_uses": r["max_uses"],
        "uses": r["uses"],
        "expires_at": r["expires_at"],
        "created_at": r["created_at"],
        "created_by": {"id": creator["id"], "display_name": creator["display_name"]} if creator else None,
        "status": _status(r),
        "mine": viewer_id == r["created_by"],
        "used_by": used_by,
    }


def create(actor, hours: float, max_uses: int, room_id: int | None, note: str | None) -> dict:
    if not _can_manage(actor):
        raise ApiError(403, "forbidden", "只有 Owner 或被授予「可添加成员」的人可以邀请")
    hours = float(hours)
    if not (0.5 <= hours <= MAX_HOURS):
        raise ApiError(400, "invalid_expiry", "有效期需在 30 分钟到 30 天之间")
    max_uses = int(max_uses)
    if not (1 <= max_uses <= MAX_USES):
        raise ApiError(400, "invalid_uses", f"使用次数需在 1 到 {MAX_USES} 之间")
    if room_id is not None:
        room = chat.require_member(int(room_id), actor["id"])
        if room["type"] != "group":
            raise ApiError(400, "not_group", "只能邀请进群聊")
    token = secrets.token_urlsafe(18)
    now = _now()
    with db.tx():
        iid = db.insert(
            "INSERT INTO invites(token_hash, created_by, room_id, note, max_uses, expires_at, created_at) VALUES(?,?,?,?,?,?,?)",
            (_hash(token), actor["id"], room_id, (note or "").strip()[:60] or None, max_uses, (now + timedelta(hours=hours)).isoformat(), now.isoformat()),
        )
        db.audit(f"user:{actor['id']}", "invite.create", room_id, invite_id=iid, max_uses=max_uses, hours=hours)
    return {"token": token, "invite": ser(db.one("SELECT * FROM invites WHERE id=?", (iid,)), actor["id"])}


def list_for(actor) -> list[dict]:
    if not _can_manage(actor):
        raise ApiError(403, "forbidden")
    if actor["role"] == "owner":
        rows = db.all("SELECT * FROM invites ORDER BY id DESC LIMIT 100")
    else:
        rows = db.all("SELECT * FROM invites WHERE created_by=? ORDER BY id DESC LIMIT 100", (actor["id"],))
    return [ser(r, actor["id"]) for r in rows]


def revoke(actor, invite_id: int) -> dict:
    r = db.one("SELECT * FROM invites WHERE id=?", (invite_id,))
    if not r or not _can_manage(actor) or (actor["role"] != "owner" and r["created_by"] != actor["id"]):
        raise ApiError(404, "invite_not_found", "邀请不存在")
    if not r["revoked_at"]:
        db.run("UPDATE invites SET revoked_at=? WHERE id=?", (_now().isoformat(), invite_id))
        db.audit(f"user:{actor['id']}", "invite.revoke", r["room_id"], invite_id=invite_id)
    return ser(db.one("SELECT * FROM invites WHERE id=?", (invite_id,)), actor["id"])


def _row(token: str):
    if not token or len(token) > 64:
        return None
    return db.one("SELECT * FROM invites WHERE token_hash=?", (_hash(token),))


def peek(token: str) -> dict:
    """未登录可调用：只返回是否有效、邀请人、群名，不泄露其他信息。"""
    r = _row(token)
    if not r:
        return {"valid": False, "reason": "not_found"}
    st = _status(r)
    if st != "active":
        return {"valid": False, "reason": st}
    creator = auth.get_user(r["created_by"])
    room = db.one("SELECT title FROM rooms WHERE id=?", (r["room_id"],)) if r["room_id"] else None
    return {
        "valid": True,
        "inviter": creator["display_name"] if creator else "",
        "room_title": (room["title"] or "群聊") if room else None,
        "expires_at": r["expires_at"],
    }


def redeem(token: str, new_user_id: int) -> None:
    """在注册事务内调用：校验并占用一次名额；需要时把新用户拉进群。"""
    r = _row(token)
    if not r or _status(r) != "active":
        raise ApiError(403, "invite_invalid", "邀请已失效，请向邀请人重新索取")
    n = db.conn().execute(
        "UPDATE invites SET uses=uses+1 WHERE id=? AND revoked_at IS NULL AND uses<max_uses", (r["id"],)
    ).rowcount
    if n != 1:
        raise ApiError(403, "invite_invalid", "邀请已失效，请向邀请人重新索取")
    db.run("INSERT INTO invite_uses(invite_id, user_id, created_at) VALUES(?,?,?)", (r["id"], new_user_id, _now().isoformat()))
    if r["room_id"]:
        room = db.one("SELECT * FROM rooms WHERE id=?", (r["room_id"],))
        inviter_in = db.one("SELECT 1 FROM room_members WHERE room_id=? AND user_id=? AND left_at IS NULL", (r["room_id"], r["created_by"]))
        if room and room["type"] == "group" and inviter_in:
            chat._add_member(r["room_id"], new_user_id, room["max_seq"])
            chat.add_event(r["room_id"], "member.changed", actor=r["created_by"])
            db.on_commit(lambda rid=r["room_id"]: chat._publish_room_update(rid))
    db.audit(f"user:{new_user_id}", "invite.redeem", r["room_id"], invite_id=r["id"], inviter=r["created_by"])
