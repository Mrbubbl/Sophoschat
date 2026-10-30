"""面向浏览器客户端的 HTTP / WebSocket 接口（会话 Cookie 认证）。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import auth, backup, calls, chat, config, db, export, hub, memory, push, security, tasks
from .errors import ApiError

COOKIE = "aichat_session"
SAFE = {"GET", "HEAD", "OPTIONS"}
router = APIRouter()


# ---------------------------------------------------------------- 认证依赖
def _origin_ok(origin: str | None, host: str | None, forwarded_host: str | None) -> bool:
    if not origin:
        return True  # 非浏览器客户端（curl/脚本）不带 Origin
    p = urlparse(origin)
    return p.netloc in {host, forwarded_host} or origin in config.get().allowed_origins


async def current_user(request: Request):
    u = auth.session_user(request.cookies.get(COOKIE))
    if not u:
        raise ApiError(401, "unauthenticated", "请先登录")
    auth.touch_session(request.cookies.get(COOKIE))
    if request.method not in SAFE and not _origin_ok(request.headers.get("origin"), request.headers.get("host"), request.headers.get("x-forwarded-host")):
        raise ApiError(403, "bad_origin", "请求来源不被允许")
    return u


async def owner_user(u=Depends(current_user)):
    if u["role"] != "owner":
        raise ApiError(403, "owner_only", "仅 Owner 可执行此操作")
    return u


async def member_manager(u=Depends(current_user)):
    """Owner，或被 Owner 授予“可以添加成员”权限的人。"""
    if u["role"] != "owner" and not u["can_add_users"]:
        raise ApiError(403, "no_permission", "你没有添加成员的权限")
    return u


def uid(u) -> int:
    return int(u["id"])


# ---------------------------------------------------------------- 请求模型
class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserIn(BaseModel):
    username: str
    display_name: str | None = None
    password: str


class RoomIn(BaseModel):
    type: str
    user_id: int | None = None
    title: str | None = None
    member_ids: list[int] = Field(default_factory=list)
    include_agent: bool = True


class RoomPatch(BaseModel):
    title: str | None = None
    consolidation_enabled: bool | None = None
    retention_days: int | None = None


class AgentToggle(BaseModel):
    enabled: bool
    history: str = "from_now"


class MemberIn(BaseModel):
    user_id: int
    history: str = "from_now"


class FlagIn(BaseModel):
    value: bool = True


class ReadIn(BaseModel):
    seq: int


class MessageIn(BaseModel):
    body: str = ""
    client_msg_id: str | None = None
    reply_to: int | None = None
    attachment_id: int | None = None


class EditIn(BaseModel):
    body: str


class ConsolidateIn(BaseModel):
    mode: str = "pending"
    from_seq: int | None = None
    to_seq: int | None = None


class ItemPatch(BaseModel):
    content: str | None = None
    state: str | None = None
    adoption: str | None = None
    topic_id: int | None = None
    reason: str | None = None


class ItemIn(BaseModel):
    topic_id: int | None = None
    content: str
    type: str = "other"


class TopicPatch(BaseModel):
    title: str | None = None
    aliases: list[str] | None = None
    overview: str | None = None
    status: str | None = None


class MergeIn(BaseModel):
    into_topic_id: int


class CredIn(BaseModel):
    label: str = ""


# ---------------------------------------------------------------- 登录
@router.get("/api/health")
async def health():
    return {"ok": True}


@router.post("/api/login")
async def login(body: LoginIn, request: Request, response: Response):
    if not _origin_ok(request.headers.get("origin"), request.headers.get("host"), request.headers.get("x-forwarded-host")):
        raise ApiError(403, "bad_origin", "请求来源不被允许")
    ip = request.client.host if request.client else ""
    user = auth.authenticate(body.username, body.password, ip)
    token = auth.create_session(user["id"], request.headers.get("user-agent", ""), ip)
    c = config.get()
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=c.cookie_secure, max_age=c.session_days * 86400, path="/")
    return {"user": auth.public_user(user)}


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(u=Depends(current_user)):
    return {"user": auth.public_user(u), "agent": tasks.agent_status()}


@router.post("/api/me/password")
async def change_password(body: PasswordIn, request: Request, response: Response, u=Depends(current_user)):
    if not auth.authenticate(u["username"], body.old_password, "pw-change"):
        raise ApiError(401, "bad_credentials")
    auth.set_password(uid(u), body.new_password)
    token = auth.create_session(uid(u), request.headers.get("user-agent", ""), request.client.host if request.client else "")
    c = config.get()
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=c.cookie_secure, max_age=c.session_days * 86400, path="/")
    return {"ok": True}


class ReactionIn(BaseModel):
    emoji: str
    on: bool = True


class PushSubIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str


class PushUnsubIn(BaseModel):
    endpoint: str


@router.get("/api/sessions")
async def sessions(request: Request, u=Depends(current_user)):
    return {"sessions": auth.list_sessions(uid(u), request.cookies.get(COOKIE))}


@router.delete("/api/sessions/{sid}")
async def revoke_one(sid: str, request: Request, u=Depends(current_user)):
    hashes = auth.revoke_session(uid(u), sid, request.cookies.get(COOKIE))
    await hub.close_sessions(hashes)
    return {"ok": True, "revoked": len(hashes)}


@router.post("/api/sessions/revoke-others")
async def revoke_others(request: Request, u=Depends(current_user)):
    hashes = auth.revoke_other_sessions(uid(u), request.cookies.get(COOKIE))
    await hub.close_sessions(hashes)
    return {"ok": True, "revoked": len(hashes)}


@router.get("/api/call/config")
async def call_config(u=Depends(current_user)):
    return calls.ice_config(uid(u))


@router.get("/api/push/key")
async def push_key(u=Depends(current_user)):
    return {"available": push.AVAILABLE, "key": push.public_key(), "subscribed": push.count_for(uid(u)) if push.AVAILABLE else 0}


@router.post("/api/push/subscribe")
async def push_subscribe(body: PushSubIn, request: Request, u=Depends(current_user)):
    if not push.AVAILABLE:
        raise ApiError(503, "push_unavailable", "服务器未启用离线推送")
    push.subscribe(uid(u), body.endpoint, body.p256dh, body.auth, request.headers.get("user-agent", ""))
    return {"ok": True}


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(body: PushUnsubIn, u=Depends(current_user)):
    push.unsubscribe(uid(u), body.endpoint)
    return {"ok": True}


@router.post("/api/messages/{message_id}/reactions")
async def react(message_id: int, body: ReactionIn, u=Depends(current_user)):
    return chat.set_reaction(message_id, uid(u), body.emoji, body.on)


AVATAR_MAX = 2 * 1024 * 1024


def _sniff_image(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _save_avatar(target_id: int, file: UploadFile) -> dict:
    data = await file.read(AVATAR_MAX + 1)
    if len(data) > AVATAR_MAX:
        raise ApiError(413, "file_too_large", "头像不能超过 2 MB")
    if not _sniff_image(data[:16]):
        raise ApiError(400, "not_image", "请选择 JPG / PNG / WebP 图片")
    d = config.get().data_dir / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{target_id}.bin").write_bytes(data)
    ver = hashlib.sha256(data).hexdigest()[:10]
    db.run("UPDATE users SET avatar=? WHERE id=?", (ver, target_id))
    return _avatar_changed(target_id)


def _avatar_changed(target_id: int) -> dict:
    u = auth.public_user(auth.get_user(target_id))
    ids = [r["id"] for r in db.all("SELECT id FROM users WHERE role!='agent' AND disabled=0")]
    hub.publish(ids, {"type": "user.updated", "user": u})
    return {"user": u}


@router.post("/api/me/avatar")
async def set_my_avatar(file: UploadFile = File(...), u=Depends(current_user)):
    return await _save_avatar(uid(u), file)


@router.delete("/api/me/avatar")
async def clear_my_avatar(u=Depends(current_user)):
    db.run("UPDATE users SET avatar=NULL WHERE id=?", (uid(u),))
    (config.get().data_dir / "avatars" / f"{uid(u)}.bin").unlink(missing_ok=True)
    return _avatar_changed(uid(u))


@router.post("/api/agent/avatar")
async def set_agent_avatar(file: UploadFile = File(...), u=Depends(owner_user)):
    aid = auth.agent_user_id()
    if not aid:
        raise ApiError(404, "agent_not_found")
    return await _save_avatar(aid, file)


@router.get("/api/avatars/{user_id}")
async def get_avatar(user_id: int, u=Depends(current_user)):
    p = config.get().data_dir / "avatars" / f"{user_id}.bin"
    row = db.one("SELECT avatar FROM users WHERE id=?", (user_id,))
    if not row or not row["avatar"] or not p.exists():
        raise ApiError(404, "no_avatar")
    mime = _sniff_image(p.read_bytes()[:16]) or "application/octet-stream"
    return FileResponse(p, media_type=mime, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=31536000, immutable"})


@router.post("/api/messages/{message_id}/transcribe")
async def transcribe(message_id: int, u=Depends(current_user)):
    return {"task": tasks.create_transcribe_task(message_id, uid(u))}


@router.get("/api/users")
async def users(u=Depends(current_user)):
    rows = db.all("SELECT * FROM users WHERE disabled=0 AND role!='agent' ORDER BY id")
    return {"users": [auth.public_user(r) for r in rows if r["id"] != u["id"]]}


@router.post("/api/users")
async def add_user(body: UserIn, u=Depends(member_manager)):
    new_id = auth.create_user(body.username, body.display_name, body.password, "user")
    chat.bootstrap_user(new_id)
    db.audit(f"user:{uid(u)}", "user.add_by", None, new_user_id=new_id)
    return {"user": auth.public_user(auth.get_user(new_id))}


# ---------------------------------------------------------------- 房间
class PermIn(BaseModel):
    can_add_users: bool


@router.patch("/api/users/{user_id}/permissions")
async def set_permissions(user_id: int, body: PermIn, u=Depends(owner_user)):
    t = auth.get_user(user_id)
    if not t or t["role"] != "user":
        raise ApiError(404, "user_not_found", "只能给普通成员设置权限")
    with db.tx():
        db.run("UPDATE users SET can_add_users=? WHERE id=?", (1 if body.can_add_users else 0, user_id))
        db.audit(f"user:{uid(u)}", "user.permission", None, target=user_id, can_add_users=body.can_add_users)
    return _avatar_changed(user_id)


@router.get("/api/rooms")
async def rooms(u=Depends(current_user)):
    return {"rooms": chat.list_rooms(uid(u))}


@router.post("/api/rooms")
async def create_room(body: RoomIn, u=Depends(current_user)):
    if body.type == "dm":
        if not body.user_id:
            raise ApiError(400, "user_required", "请选择对方")
        rid = chat.create_dm(uid(u), body.user_id, body.include_agent)
    elif body.type == "group":
        rid = chat.create_group(uid(u), body.title or "", body.member_ids, body.include_agent)
    else:
        raise ApiError(400, "invalid_type", "type 必须是 dm 或 group")
    return {"room": chat.get_room(rid, uid(u))}


@router.get("/api/rooms/{room_id}")
async def get_room(room_id: int, u=Depends(current_user)):
    return {"room": chat.get_room(room_id, uid(u))}


@router.patch("/api/rooms/{room_id}")
async def patch_room(room_id: int, body: RoomPatch, u=Depends(current_user)):
    return {"room": chat.update_room(room_id, uid(u), body.model_dump(exclude_unset=True))}


@router.post("/api/rooms/{room_id}/agent")
async def room_agent(room_id: int, body: AgentToggle, u=Depends(current_user)):
    return {"room": chat.set_agent_in_room(room_id, uid(u), body.enabled, body.history)}


@router.post("/api/rooms/{room_id}/members")
async def room_add_member(room_id: int, body: MemberIn, u=Depends(current_user)):
    return {"room": chat.add_member(room_id, uid(u), body.user_id, body.history)}


@router.delete("/api/rooms/{room_id}/members/{user_id}")
async def room_remove_member(room_id: int, user_id: int, u=Depends(current_user)):
    chat.remove_member(room_id, uid(u), user_id)
    return {"ok": True}


@router.post("/api/rooms/{room_id}/archive")
async def room_archive(room_id: int, body: FlagIn, u=Depends(current_user)):
    return {"room": chat.set_member_flag(room_id, uid(u), "archived", body.value)}


@router.post("/api/rooms/{room_id}/mute")
async def room_mute(room_id: int, body: FlagIn, u=Depends(current_user)):
    return {"room": chat.set_member_flag(room_id, uid(u), "muted", body.value)}


@router.post("/api/rooms/{room_id}/read")
async def room_read(room_id: int, body: ReadIn, u=Depends(current_user)):
    return chat.mark_read(room_id, uid(u), body.seq)


# ---------------------------------------------------------------- 消息
@router.get("/api/rooms/{room_id}/messages")
async def list_messages(room_id: int, before_seq: int | None = None, after_seq: int | None = None, around_seq: int | None = None, from_time: str | None = None, limit: int = 50, q: str | None = None, sender_id: int | None = None, u=Depends(current_user)):
    return chat.get_messages(room_id, uid(u), before_seq=before_seq, after_seq=after_seq, around_seq=around_seq, from_time=from_time, limit=limit, q=q, sender_id=sender_id)


@router.post("/api/rooms/{room_id}/messages")
async def post_message(room_id: int, body: MessageIn, response: Response, u=Depends(current_user)):
    msg, dup = chat.send_message(room_id, uid(u), body.body, body.client_msg_id, body.reply_to, body.attachment_id)
    if dup:
        response.status_code = 200
    return {"message": msg, "duplicate": dup}


@router.patch("/api/messages/{message_id}")
async def patch_message(message_id: int, body: EditIn, u=Depends(current_user)):
    return {"message": chat.edit_message(message_id, uid(u), body.body)}


@router.delete("/api/messages/{message_id}")
async def del_message(message_id: int, u=Depends(current_user)):
    chat.delete_message(message_id, uid(u))
    return {"ok": True}


@router.get("/api/messages/{message_id}/locate")
async def locate(message_id: int, u=Depends(current_user)):
    return chat.locate_message(message_id, uid(u))


@router.post("/api/messages/{message_id}/star")
async def star(message_id: int, body: FlagIn, u=Depends(current_user)):
    chat.set_star(uid(u), message_id, body.value)
    return {"ok": True}


@router.get("/api/rooms/{room_id}/stars")
async def stars(room_id: int, u=Depends(current_user)):
    return {"messages": chat.list_stars(uid(u), room_id)}


@router.get("/api/search")
async def search(q: str, room_id: int | None = None, sender_id: int | None = None, from_time: str | None = None, to_time: str | None = None, limit: int = 50, u=Depends(current_user)):
    return {"messages": chat.search_messages(uid(u), q, room_id, sender_id, from_time, to_time, limit)}


# ---------------------------------------------------------------- 附件
_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
_AUDIO_MAGIC = {
    "audio/webm": lambda h: h.startswith(b"\x1a\x45\xdf\xa3"),
    "audio/ogg": lambda h: h.startswith(b"OggS"),
    "audio/mp4": lambda h: h[4:8] == b"ftyp",
    "audio/x-m4a": lambda h: h[4:8] == b"ftyp",
    "audio/aac": lambda h: h[:2] in (b"\xff\xf1", b"\xff\xf9") or h[4:8] == b"ftyp",
    "audio/mpeg": lambda h: h.startswith(b"ID3") or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0),
    "audio/wav": lambda h: h.startswith(b"RIFF"),
}


@router.post("/api/rooms/{room_id}/attachments")
async def upload(room_id: int, file: UploadFile = File(...), duration_ms: int | None = Form(None), u=Depends(current_user)):
    chat.require_member(room_id, uid(u))
    path, rel = chat.new_attachment_path(room_id)
    limit = config.get().max_upload_mb * 1024 * 1024
    size, h, head = 0, hashlib.sha256(), b""
    with open(path, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                f.close()
                path.unlink(missing_ok=True)
                raise ApiError(413, "file_too_large", f"文件不能超过 {config.get().max_upload_mb} MB")
            if not head:
                head = chunk[:16]
            h.update(chunk)
            f.write(chunk)
    if size == 0:
        path.unlink(missing_ok=True)
        raise ApiError(400, "empty_file", "文件为空")
    mime = (file.content_type or "application/octet-stream").split(";")[0].strip().lower()
    if mime in _MAGIC and not any(head.startswith(m) for m in _MAGIC[mime]):
        mime = "application/octet-stream"  # 声明为图片但内容不是，按普通文件处理
    if mime in _AUDIO_MAGIC and not _AUDIO_MAGIC[mime](head):
        mime = "application/octet-stream"
    if mime not in chat.AUDIO_MIMES:
        duration_ms = None
    elif duration_ms is not None:
        duration_ms = max(0, min(int(duration_ms), 15 * 60 * 1000))
    att = chat.register_attachment(room_id, uid(u), file.filename or "file", mime, size, rel, h.hexdigest(), duration_ms)
    return {"attachment": att}


@router.get("/api/attachments/{att_id}")
async def download(att_id: int, download: int = 0, u=Depends(current_user)):
    a = chat.get_attachment(att_id, uid(u))
    p = config.get().data_dir / a["path"]
    if not p.exists():
        raise ApiError(404, "attachment_missing", "文件已丢失")
    inline = (a["mime"] in chat.IMAGE_MIMES or (a["mime"] in chat.AUDIO_MIMES and a["duration_ms"] is not None)) and not download
    return FileResponse(
        p,
        media_type=a["mime"] if inline else "application/octet-stream",
        filename=a["filename"],
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "private, max-age=3600"},
    )


# ---------------------------------------------------------------- Agent 任务（用户视角）
@router.get("/api/rooms/{room_id}/tasks")
async def room_tasks(room_id: int, u=Depends(current_user)):
    chat.require_member(room_id, uid(u))
    return {"tasks": tasks.room_tasks(room_id)}


@router.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int, u=Depends(current_user)):
    return {"task": tasks.retry_chat_task(task_id, uid(u))}


@router.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, u=Depends(current_user)):
    return {"task": tasks.cancel_task(task_id, uid(u))}


@router.post("/api/rooms/{room_id}/consolidate")
async def consolidate(room_id: int, body: ConsolidateIn, u=Depends(current_user)):
    return {"task": tasks.request_consolidation(room_id, uid(u), body.mode, body.from_seq, body.to_seq)}


@router.get("/api/rooms/{room_id}/consolidation")
async def consolidation_state(room_id: int, u=Depends(current_user)):
    chat.require_member(room_id, uid(u))
    active = db.one("SELECT * FROM agent_tasks WHERE room_id=? AND kind='consolidate' AND status IN ('queued','leased') ORDER BY id DESC LIMIT 1", (room_id,))
    last = db.one("SELECT * FROM agent_tasks WHERE room_id=? AND kind='consolidate' AND status IN ('done','failed') ORDER BY id DESC LIMIT 1", (room_id,))
    return {"pending_events": tasks.pending_event_count(room_id), "active": tasks.ser_task(active) if active else None, "last": tasks.ser_task(last) if last else None}


# ---------------------------------------------------------------- 主题记忆（用户视角）
@router.get("/api/rooms/{room_id}/topics")
async def topics(room_id: int, status: str | None = "active", u=Depends(current_user)):
    chat.require_member(room_id, uid(u))
    return {"topics": memory.list_topics(room_id, status or None), "pending_items": memory.pending_items(room_id)}


@router.get("/api/topics/{topic_id}")
async def topic_detail(topic_id: int, history: int = 0, u=Depends(current_user)):
    t = db.one("SELECT room_id FROM topics WHERE id=?", (topic_id,))
    if not t:
        raise ApiError(404, "topic_not_found", "主题不存在")
    chat.require_member(t["room_id"], uid(u))
    return {"topic": memory.get_topic(topic_id, t["room_id"], bool(history))}


@router.patch("/api/topics/{topic_id}")
async def topic_patch(topic_id: int, body: TopicPatch, u=Depends(current_user)):
    return {"topic": memory.human_update_topic(uid(u), topic_id, body.model_dump(exclude_unset=True))}


@router.post("/api/topics/{topic_id}/merge")
async def topic_merge(topic_id: int, body: MergeIn, u=Depends(current_user)):
    return {"topic": memory.human_merge_topics(uid(u), topic_id, body.into_topic_id)}


@router.post("/api/rooms/{room_id}/memory/items")
async def item_create(room_id: int, body: ItemIn, u=Depends(current_user)):
    return {"item": memory.human_create_item(uid(u), room_id, body.topic_id, body.content, body.type)}


@router.patch("/api/memory/items/{item_id}")
async def item_patch(item_id: int, body: ItemPatch, u=Depends(current_user)):
    return {"item": memory.human_update_item(uid(u), item_id, body.model_dump(exclude_unset=True))}


@router.delete("/api/memory/items/{item_id}")
async def item_delete(item_id: int, u=Depends(current_user)):
    memory.human_delete_item(uid(u), item_id)
    return {"ok": True}


@router.get("/api/memory/items/{item_id}/history")
async def item_hist(item_id: int, u=Depends(current_user)):
    it = db.one("SELECT room_id FROM memory_items WHERE id=?", (item_id,))
    if not it:
        raise ApiError(404, "item_not_found", "记忆条目不存在")
    chat.require_member(it["room_id"], uid(u))
    return {"history": memory.item_history(item_id, it["room_id"])}


@router.get("/api/rooms/{room_id}/summaries")
async def summaries(room_id: int, u=Depends(current_user)):
    chat.require_member(room_id, uid(u))
    return {"summaries": memory.list_summaries(room_id)}


# ---------------------------------------------------------------- 导出
@router.get("/api/rooms/{room_id}/export")
async def export_room(room_id: int, format: str = "md", from_time: str | None = None, to_time: str | None = None, include_mem: int = Query(0, alias="memory"), attachments: int = 0, u=Depends(current_user)):
    data, name, mime = export.export_room(room_id, uid(u), format, from_time, to_time, bool(include_mem), bool(attachments))
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- Agent 接入（Owner）
@router.get("/api/agent/status")
async def agent_status(u=Depends(current_user)):
    return {"status": tasks.agent_status()}


@router.get("/api/agent/credentials")
async def agent_credentials(u=Depends(owner_user)):
    return {"credentials": auth.list_credentials()}


@router.post("/api/agent/credentials")
async def agent_issue(body: CredIn, u=Depends(owner_user)):
    cid, token = auth.issue_credential(body.label)
    return {"id": cid, "token": token, "note": "令牌只显示这一次，请立即保存"}


@router.delete("/api/agent/credentials/{cred_id}")
async def agent_revoke(cred_id: int, u=Depends(owner_user)):
    if not auth.revoke_credential(cred_id):
        raise ApiError(404, "credential_not_found", "凭证不存在或已撤销")
    return {"ok": True}


@router.post("/api/agent/selfcheck")
async def agent_selfcheck(u=Depends(owner_user)):
    """接入自检：在 Owner 的 Agent 私聊里发一条测试消息，触发一次完整的“领取任务→回复”闭环。"""
    st = tasks.agent_status()
    checks = [
        {"name": "已创建凭证", "ok": st["state"] != "unconfigured"},
        {"name": "Bridge 心跳在线", "ok": st["state"] == "online"},
    ]
    if not all(c["ok"] for c in checks):
        return {"ok": False, "checks": checks}
    rid = chat.ensure_agent_dm(uid(u))
    msg, _ = chat.send_message(rid, uid(u), "【接入自检】请回复一句话确认你能收到这条消息。", f"selfcheck-{db.now()}")
    return {"ok": True, "checks": checks, "room_id": rid, "message_id": msg["id"]}


@router.get("/agent-guide.md")
async def agent_guide(request: Request):
    p = Path(__file__).parent / "AGENT_GUIDE.md"
    text = p.read_text(encoding="utf-8").replace("{{GUIDE_VERSION}}", tasks.GUIDE_VERSION).replace("{{PLATFORM_VERSION}}", tasks.PLATFORM_VERSION).replace("{{BASE_URL}}", str(request.base_url).rstrip("/"))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@router.get("/api/admin/doctor")
async def admin_doctor(u=Depends(owner_user)):
    return {"issues": backup.doctor()}


# ---------------------------------------------------------------- WebSocket
@router.websocket("/ws")
async def ws(websocket: WebSocket):
    if not _origin_ok(websocket.headers.get("origin"), websocket.headers.get("host"), websocket.headers.get("x-forwarded-host")):
        await websocket.close(code=4403)
        return
    user = auth.session_user(websocket.cookies.get(COOKIE))
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    user_id = user["id"]
    await hub.connect(user_id, websocket, security.token_hash(websocket.cookies.get(COOKIE)))
    try:
        await websocket.send_json({"type": "hello", "user": auth.public_user(user), "agent": tasks.agent_status()})
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                continue
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            elif data.get("type") == "typing" and isinstance(data.get("room_id"), int):
                chat.typing(data["room_id"], user_id)
            elif data.get("type") == "call":
                calls.handle(user_id, data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.disconnect(user_id, websocket)
        if user_id not in hub.online_users():
            calls.on_disconnect(user_id)
