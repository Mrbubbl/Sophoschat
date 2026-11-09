"""Agent 任务队列：聊天互动任务 + 后台整理任务；租约、过期、重试、状态。"""
from __future__ import annotations

import json
import secrets
from typing import Any

from . import auth, config, db, hub, security
from .errors import ApiError

GUIDE_VERSION = "0.8.0"
PLATFORM_VERSION = "0.4.0"


# ---------------------------------------------------------------- 序列化与广播
def ser_task(t) -> dict:
    return {
        "id": t["id"],
        "room_id": t["room_id"],
        "kind": t["kind"],
        "status": t["status"],
        "trigger_message_id": t["trigger_message_id"],
        "attempts": t["attempts"],
        "created_at": t["created_at"],
        "expires_at": t["expires_at"],
        "finished_at": t["finished_at"],
        "error": t["error"],
        "profile": "memory" if t["kind"] == "consolidate" else t["profile"],
        "initiator_id": t["initiator_id"],
        "progress": t["progress"],
        "parent_task_id": t["parent_task_id"],
        "base_version_id": t["base_version_id"],
        "result_message_id": t["result_message_id"],
    }


def _humans(room_id: int) -> list[int]:
    return [r["user_id"] for r in db.all(
        "SELECT m.user_id FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.left_at IS NULL AND u.role!='agent'",
        (room_id,),
    )]


def _publish_task(task_id: int) -> None:
    def push():
        t = db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        if t and t["kind"] == "chat":
            hub.publish(_humans(t["room_id"]), {"type": "task.updated", "task": ser_task(t)})

    db.on_commit(push)


def _wake() -> None:
    db.on_commit(hub.wake_agent)


# ---------------------------------------------------------------- 创建
def maybe_create_chat_task(room, msg, mention_ids: list[int]) -> int | None:
    """群聊只有明确 @Agent 才触发；Agent 私聊中用户的每条消息都触发。Agent 自己的消息永不触发。"""
    agent = db.one(
        "SELECT m.user_id FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND u.role='agent' AND m.left_at IS NULL AND u.disabled=0",
        (room["id"],),
    )
    if not agent:
        return None
    directed = db.one("SELECT sender_id FROM messages WHERE id=? AND room_id=?", (msg["reply_to"], room["id"])) if msg["reply_to"] else None
    triggered = room["type"] == "agent_dm" or agent["user_id"] in mention_ids or (directed and directed["sender_id"] == agent["user_id"])
    if not triggered:
        return None
    import re
    # 消息以 /pro 开头（可在 @Agent 之后）= 这一条交给“高级”接入处理；未配置高级接入时自动忽略
    pro = bool(re.match(r"^\s*(?:@\S+\s+)*/pro\b", (msg["body"] if "body" in msg.keys() else "") or ""))
    tid = db.insert(
        "INSERT INTO agent_tasks(room_id, kind, trigger_message_id, trigger_message_version, status, created_at, expires_at, params) VALUES(?,?,?,?,?,?,?,?)",
        (room["id"], "chat", msg["id"], msg["version"], "queued", db.now(), db.after(config.get().chat_task_ttl), db.jl({"tier": "pro"} if pro else {})),
    )
    db.run("UPDATE agent_tasks SET initiator_id=? WHERE id=?", (msg["sender_id"], tid))
    if directed:
        parent = db.one("SELECT artifact_version_id FROM messages WHERE id=?", (msg["reply_to"],))
        if parent and parent["artifact_version_id"]:
            from . import artifacts
            base = artifacts.authorized(parent["artifact_version_id"], msg["sender_id"])
            if base["initiator_id"] == msg["sender_id"] or auth.get_user(msg["sender_id"])["role"] == "owner":
                db.run("UPDATE agent_tasks SET profile='delivery',base_version_id=?,parent_task_id=?,expires_at=? WHERE id=?", (base["id"],base["task_id"],db.after(86400),tid))
            else:
                db.run("UPDATE agent_tasks SET status='failed',error='task_controller_only',finished_at=? WHERE id=?", (db.now(),tid))
    _publish_task(tid)
    _wake()
    return tid


def create_transcribe_task(message_id: int, user_id: int) -> dict:
    """用户长按语音消息选“转文字”：交给 Agent 做语音识别，结果写回附件（不在聊天里发消息）。"""
    from . import chat

    if not config.get().allow_legacy_agent:
        raise ApiError(501, "transcription_not_available", "当前 SDK 模式尚未提供语音转写；语音消息仍可正常收发")

    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=?", (message_id,))
        if not m or m["deleted_at"]:
            raise ApiError(404, "message_not_found")
        room = chat.require_member(m["room_id"], user_id)
        a = db.one("SELECT * FROM attachments WHERE id=? AND deleted_at IS NULL", (m["attachment_id"],)) if m["attachment_id"] else None
        if not a or a["mime"] not in chat.AUDIO_MIMES or a["duration_ms"] is None:
            raise ApiError(400, "not_voice", "这不是语音消息")
        if m["seq"] <= room["history_from_seq"]:
            raise ApiError(404, "message_not_found")
        if not chat.agent_member_id(room["id"]):
            raise ApiError(409, "agent_not_in_room", "这个会话里没有 Agent")
        pending = db.one("SELECT id FROM agent_tasks WHERE trigger_message_id=? AND kind='chat' AND status IN ('queued','leased') AND params LIKE '%transcribe%'", (message_id,))
        if pending:
            return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (pending["id"],)))
        tid = db.insert(
            "INSERT INTO agent_tasks(room_id, kind, trigger_message_id, trigger_message_version, status, created_at, expires_at, params) VALUES(?,?,?,?,?,?,?,?)",
            (room["id"], "chat", m["id"], m["version"], "queued", db.now(), db.after(config.get().chat_task_ttl), db.jl({"intent": "transcribe"})),
        )
        _publish_task(tid)
        _wake()
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (tid,)))


def retry_chat_task(task_id: int, user_id: int) -> dict:
    from . import chat

    with db.tx():
        old = db.one("SELECT * FROM agent_tasks WHERE id=? AND kind='chat'", (task_id,))
        if not old:
            raise ApiError(404, "task_not_found")
        room = chat.require_member(old["room_id"], user_id)
        require_controller(old, user_id)
        if old["status"] not in ("expired", "failed", "cancelled"):
            raise ApiError(409, "not_retryable", "该任务仍在处理中或已完成")
        m = db.one("SELECT * FROM messages WHERE id=?", (old["trigger_message_id"],))
        if not m or m["deleted_at"]:
            raise ApiError(409, "source_deleted", "原消息已删除，无法重试")
        if not chat.agent_member_id(room["id"]):
            raise ApiError(409, "agent_not_in_room", "Agent 已不在该会话中")
        if old["profile"] == "delivery" and db.one("SELECT 1 FROM artifact_versions WHERE task_id=?", (task_id,)):
            # Resume publication instead of making a second logical file.
            db.run("UPDATE agent_tasks SET status='queued',finished_at=NULL,error=NULL,lease_owner=NULL,lease_credential_id=NULL,lease_until=NULL,lease_generation=NULL,not_before=NULL,max_attempts=attempts+3,expires_at=? WHERE id=?", (db.after(86400),task_id))
            _publish_task(task_id)
            _wake()
            return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,)))
        tid = db.insert(
            "INSERT INTO agent_tasks(room_id, kind, trigger_message_id, trigger_message_version, status, created_at, expires_at, params) VALUES(?,?,?,?,?,?,?,?)",
            (room["id"], "chat", m["id"], m["version"], "queued", db.now(), db.after(config.get().chat_task_ttl), db.jl({"retry_of": task_id})),
        )
        params = {**db.jd(old["params"], {}), "retry_of": task_id}
        db.run("UPDATE agent_tasks SET initiator_id=?,profile=?,base_version_id=?,parent_task_id=?,params=? WHERE id=?", (user_id,old["profile"],old["base_version_id"],task_id,db.jl(params),tid))
        _publish_task(tid)
        _wake()
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (tid,)))


def cancel_task(task_id: int, user_id: int) -> dict:
    from . import chat

    with db.tx():
        t = db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
        if not t:
            raise ApiError(404, "task_not_found")
        chat.require_member(t["room_id"], user_id)
        require_controller(t, user_id)
        if t["status"] in ("queued", "leased"):
            db.run("UPDATE agent_tasks SET status='cancelled', finished_at=?, error='cancelled_by_user' WHERE id=?", (db.now(), task_id))
            _publish_task(task_id)
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,)))


def require_controller(t, user_id: int) -> None:
    user = auth.get_user(user_id)
    initiator = t["initiator_id"]
    if initiator is None and t["trigger_message_id"]:
        source = db.one("SELECT sender_id FROM messages WHERE id=?", (t["trigger_message_id"],))
        initiator = source["sender_id"] if source else None
    if user["role"] != "owner" and user_id != initiator:
        raise ApiError(403, "task_controller_only", "只有任务发起人或 Owner 可以改变、重试或停止这个任务")


def cancel_for_message(message_id: int, reason: str) -> None:
    rows = db.all("SELECT id FROM agent_tasks WHERE trigger_message_id=? AND status IN ('queued','leased')", (message_id,))
    for r in rows:
        db.run("UPDATE agent_tasks SET status='cancelled', finished_at=?, error=? WHERE id=?", (db.now(), reason, r["id"]))
        _publish_task(r["id"])


def cancel_room_tasks(room_id: int, reason: str) -> None:
    rows = db.all("SELECT id FROM agent_tasks WHERE room_id=? AND status IN ('queued','leased')", (room_id,))
    for r in rows:
        db.run("UPDATE agent_tasks SET status='cancelled', finished_at=?, error=? WHERE id=?", (db.now(), reason, r["id"]))
        _publish_task(r["id"])


def room_tasks(room_id: int, limit: int = 30) -> list[dict]:
    rows = db.all("SELECT * FROM agent_tasks WHERE room_id=? AND kind='chat' ORDER BY id DESC LIMIT ?", (room_id, limit))
    return [ser_task(r) for r in rows]


# ---------------------------------------------------------------- 整理任务
def _cursor(room_id: int) -> int:
    r = db.one("SELECT last_event_id FROM cursors WHERE consumer='consolidate' AND room_id=?", (room_id,))
    return r["last_event_id"] if r else 0


def pending_event_count(room_id: int) -> int:
    return db.one("SELECT COUNT(*) AS c FROM events WHERE room_id=? AND id>? AND type IN ('message.created','message.edited','message.deleted')", (room_id, _cursor(room_id)))["c"]


def request_consolidation(room_id: int, user_id: int, mode: str = "pending", from_seq: int | None = None, to_seq: int | None = None) -> dict:
    from . import chat

    room = chat.require_member(room_id, user_id)
    if not chat.agent_member_id(room_id):
        raise ApiError(409, "agent_not_in_room", "Agent 不在该会话中，无法整理")
    with db.tx():
        active = db.one("SELECT id FROM agent_tasks WHERE room_id=? AND kind='consolidate' AND status IN ('queued','leased')", (room_id,))
        if active:
            raise ApiError(409, "already_running", "已有整理任务在排队或处理中", task_id=active["id"])
        if mode == "reprocess":
            lo = int(from_seq or 1)
            hi = int(to_seq or room["max_seq"])
            if hi < lo:
                raise ApiError(400, "invalid_range", "范围不合法")
            params = {"mode": "reprocess", "from_seq": lo, "to_seq": hi, "requested_by": user_id}
        else:
            cur = _cursor(room_id)
            if pending_event_count(room_id) == 0:
                raise ApiError(409, "nothing_pending", "没有需要整理的新内容")
            params = {"mode": "pending", "event_from": cur, "event_to": _batch_end(room_id, cur), "requested_by": user_id}
        tid = db.insert(
            "INSERT INTO agent_tasks(room_id, kind, profile, initiator_id, status, created_at, params) VALUES(?, 'consolidate', 'memory', ?, 'queued', ?, ?)",
            (room_id, user_id, db.now(), db.jl(params)),
        )
        db.audit(f"user:{user_id}", "consolidate.request", room_id, mode=mode)
        _wake()
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (tid,)))


def _batch_end(room_id: int, cursor: int) -> int:
    rows = db.all("SELECT id FROM events WHERE room_id=? AND id>? ORDER BY id LIMIT ?", (room_id, cursor, config.get().consolidate_batch_events))
    return rows[-1]["id"] if rows else cursor


def schedule_consolidation() -> int:
    """按“静默一段时间或达到最长等待”的规则，为有新内容的 Room 创建整理任务。"""
    c = config.get()
    created = 0
    rooms = db.all("SELECT id FROM rooms WHERE consolidation_enabled=1")
    for r in rooms:
        rid = r["id"]
        if db.one("SELECT 1 FROM agent_tasks WHERE room_id=? AND kind='consolidate' AND status IN ('queued','leased')", (rid,)):
            continue
        from . import chat

        if not chat.agent_member_id(rid):
            continue
        cur = _cursor(rid)
        first = db.one("SELECT created_at FROM events WHERE room_id=? AND id>? AND type IN ('message.created','message.edited','message.deleted') ORDER BY id LIMIT 1", (rid, cur))
        if not first:
            continue
        last = db.one("SELECT created_at FROM events WHERE room_id=? AND type IN ('message.created','message.edited','message.deleted') ORDER BY id DESC LIMIT 1", (rid,))
        quiet = last["created_at"] <= db.ago(c.consolidate_quiet)
        overdue = first["created_at"] <= db.ago(c.consolidate_max_wait)
        if not (quiet or overdue):
            continue
        with db.tx():
            end = _batch_end(rid, cur)
            db.insert(
                "INSERT INTO agent_tasks(room_id, kind, status, created_at, params) VALUES(?, 'consolidate', 'queued', ?, ?)",
                (rid, db.now(), db.jl({"mode": "pending", "event_from": cur, "event_to": end})),
            )
            _wake()
        created += 1
    return created


# ---------------------------------------------------------------- 维护循环
def maintenance() -> dict:
    """过期、租约回收、整理调度。由调度器周期调用，也可在测试中直接调用。"""
    stats = {"expired": 0, "requeued": 0, "failed": 0, "consolidations": 0}
    now = db.now()
    with db.tx():
        for t in db.all("SELECT id FROM agent_tasks WHERE status='queued' AND kind='chat' AND expires_at IS NOT NULL AND expires_at<?", (now,)):
            db.run("UPDATE agent_tasks SET status='expired', finished_at=?, error='expired' WHERE id=?", (now, t["id"]))
            _publish_task(t["id"])
            stats["expired"] += 1
        for t in db.all("SELECT * FROM agent_tasks WHERE status='leased' AND lease_until<?", (now,)):
            if t["kind"] == "chat" and t["expires_at"] and t["expires_at"] < now:
                db.run("UPDATE agent_tasks SET status='expired', finished_at=?, error='expired' WHERE id=?", (now, t["id"]))
                stats["expired"] += 1
            elif t["attempts"] >= t["max_attempts"]:
                db.run("UPDATE agent_tasks SET status='failed', finished_at=?, error='lease_lost' WHERE id=?", (now, t["id"]))
                stats["failed"] += 1
            else:
                db.run("UPDATE agent_tasks SET status='queued', lease_owner=NULL, lease_credential_id=NULL, lease_until=NULL WHERE id=?", (t["id"],))
                stats["requeued"] += 1
                _wake()
            _publish_task(t["id"])
    stats["consolidations"] = schedule_consolidation()
    return stats


# ---------------------------------------------------------------- 租约
def _signed(t) -> str:
    c = config.get()
    purpose = "chat" if t["kind"] == "chat" else "consolidate"
    return security.sign_task_token(c.secret, t["id"], t["room_id"], purpose, 86400,
                                    generation=t["lease_generation"], credential_id=t["lease_credential_id"], owner=t["lease_owner"])


def lease_next(cred, owner: str = "", *, engine: str = "legacy", profiles: tuple[str, ...] = ()) -> dict | None:
    from . import chat, runtime

    s = runtime.settings()
    sdk = engine == "claude-sdk"
    if sdk:
        runtime.require_service(cred)
        if not (s["enabled"] and s["configured"]):
            return None
    elif not config.get().allow_legacy_agent:
        raise ApiError(403, "sdk_required", "请使用平台服务端 Claude Agent SDK，外部模型适配器已停用")
    with db.tx():
        now = db.now()
        candidates = db.all(
            "SELECT * FROM agent_tasks WHERE status='queued' AND (expires_at IS NULL OR expires_at>?) AND (not_before IS NULL OR not_before<=?) "
            "ORDER BY CASE WHEN kind='consolidate' AND created_at<? THEN 0 WHEN kind='chat' AND profile='chat' THEN 1 WHEN profile='delivery' THEN 2 ELSE 3 END,id",
            (now, now, db.ago(s["memory_max_delay_seconds"])),
        )
        for t in candidates:
            p = runtime.profile(t)
            if profiles and p not in profiles:
                continue
            if p == "memory" and sdk and not s["memory_enabled"]:
                continue
            reason = None
            if not chat.agent_member_id(t["room_id"]):
                reason = "agent_not_in_room"
            elif t["kind"] == "chat":
                m = db.one("SELECT deleted_at, version, sender_id FROM messages WHERE id=?", (t["trigger_message_id"],))
                if not m or m["deleted_at"]:
                    reason = "source_deleted"
                elif not db.one("SELECT 1 FROM room_members WHERE room_id=? AND user_id=? AND left_at IS NULL", (t["room_id"], m["sender_id"])):
                    reason = "initiator_left_room"
            else:
                room = db.one("SELECT consolidation_enabled FROM rooms WHERE id=?", (t["room_id"],))
                params = db.jd(t["params"], {})
                if not room or (not room["consolidation_enabled"] and not params.get("requested_by")):
                    reason = "consolidation_disabled"
            if reason:
                db.run("UPDATE agent_tasks SET status='cancelled', finished_at=?, error=? WHERE id=?", (now, reason, t["id"]))
                _publish_task(t["id"])
                continue
            admitted = None
            if sdk:
                try:
                    admitted = runtime.admission(t, s)
                except ApiError as e:
                    if e.code == "agent_budget_exhausted" and t["kind"] == "consolidate":
                        continue  # 后台整理遇到额度不足：保持排队等额度恢复，别失败后又被调度器每 10 秒重建
                    db.run("UPDATE agent_tasks SET status='failed', finished_at=?, error=? WHERE id=?", (now, e.code, t["id"]))
                    _publish_task(t["id"])
                    continue
                if admitted is None:
                    continue
            db.run(
                "UPDATE agent_tasks SET status='leased', profile=?, lease_owner=?, lease_credential_id=?, lease_until=?, lease_generation=?, attempts=attempts+1 WHERE id=?",
                (p, (owner or "")[:60], cred["id"], db.after(config.get().lease_seconds), secrets.token_urlsafe(24), t["id"]),
            )
            if admitted:
                db.run("UPDATE agent_tasks SET session_key=?,scope_hash=?,config_revision=? WHERE id=?", (admitted["session_key"],admitted["scope_hash"],s["revision"],t["id"]))
            _publish_task(t["id"])
            t = db.one("SELECT * FROM agent_tasks WHERE id=?", (t["id"],))
            if admitted:
                runtime.begin_attempt(t, admitted, s)
            return {"task": ser_task(t) | {"params": db.jd(t["params"], {}), "lease_until": t["lease_until"]}, "task_token": _signed(t), "purpose": t["kind"]}
        return None


def _owned(task_id: int, cred, proof: str) -> Any:
    t = db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,))
    if not t:
        raise ApiError(404, "task_not_found")
    if t["status"] != "leased" or t["lease_credential_id"] != cred["id"]:
        raise ApiError(409, "lease_not_held", "任务租约已失效", task_status=t["status"])
    checked, checked_cred, _ = verify_tool_token(proof, ("chat", "consolidate"))
    if checked["id"] != task_id or checked_cred["id"] != cred["id"]:
        raise ApiError(409, "lease_not_held", "执行尝试不匹配")
    return t


def renew(task_id: int, cred, proof: str = "") -> dict:
    with db.tx():
        t = _owned(task_id, cred, proof)
        db.run("UPDATE agent_tasks SET lease_until=? WHERE id=?", (db.after(config.get().lease_seconds), task_id))
        return {"lease_until": db.one("SELECT lease_until FROM agent_tasks WHERE id=?", (task_id,))["lease_until"], "kind": t["kind"]}


def complete(task_id: int, cred, result_message_id: int | None = None, proof: str = "") -> dict:
    with db.tx():
        t = _owned(task_id, cred, proof)
        if t["config_revision"] is not None:
            from . import runtime
            runtime.validate_completion(t)
        if t["kind"] == "consolidate":
            params = db.jd(t["params"], {})
            if params.get("mode", "pending") == "pending":
                if _cursor(t["room_id"]) < params["event_to"]:
                    raise ApiError(409, "not_committed", "整理结果尚未提交（即使没有可记的内容，也要提交一个空批次来推进游标）")
        db.run(
            "UPDATE agent_tasks SET status='done', finished_at=?, result_message_id=?, error=NULL WHERE id=?",
            (db.now(), result_message_id, task_id),
        )
        db.set_kv("last_success_at", db.now())
        if t["kind"] == "chat":
            db.set_kv("last_reply_at", db.now())
        _publish_task(task_id)
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,)))


def fail(task_id: int, cred, error: str, retry: bool = True, proof: str = "") -> dict:
    with db.tx():
        t = _owned(task_id, cred, proof)
        err = (error or "error")[:500]
        db.set_kv("last_error", json.dumps({"at": db.now(), "task_id": task_id, "error": err}, ensure_ascii=False))
        if retry and t["attempts"] < t["max_attempts"] and not (t["kind"] == "chat" and t["expires_at"] and t["expires_at"] < db.now()):
            db.run("UPDATE agent_tasks SET status='queued', lease_owner=NULL, lease_credential_id=NULL, lease_until=NULL, error=? WHERE id=?", (err, task_id))
            if t["config_revision"] is not None:
                db.run("UPDATE agent_tasks SET not_before=? WHERE id=?", (db.after(min(60, 2 ** t["attempts"])), task_id))
            _wake()
        else:
            db.run("UPDATE agent_tasks SET status='failed', finished_at=?, error=? WHERE id=?", (db.now(), err, task_id))
        _publish_task(task_id)
        return ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (task_id,)))


# ---------------------------------------------------------------- 任务令牌校验（工具接口用）
def verify_tool_token(token: str, purposes: tuple[str, ...]):
    """校验任务令牌并回查数据库：任务仍被该凭证持有、凭证未撤销、Agent 仍在会话里。"""
    from . import chat

    payload = security.verify_task_token(config.get().secret, token or "")
    if not payload:
        raise ApiError(401, "invalid_task_token", "任务令牌无效或已过期")
    t = db.one("SELECT * FROM agent_tasks WHERE id=?", (payload["t"],))
    if not t or t["room_id"] != payload["r"]:
        raise ApiError(401, "invalid_task_token", "任务令牌无效")
    if t["status"] != "leased" or not t["lease_until"] or t["lease_until"] < db.now():
        raise ApiError(409, "lease_not_held", "任务租约已失效，请重新领取", task_status=t["status"])
    if (not payload.get("g") or payload["g"] != t["lease_generation"] or payload.get("c") != t["lease_credential_id"] or payload.get("o") != t["lease_owner"]):
        raise ApiError(409, "lease_not_held", "这个执行尝试已经失效")
    cred = db.one("SELECT * FROM credentials WHERE id=? AND revoked_at IS NULL", (t["lease_credential_id"],))
    if not cred:
        raise ApiError(401, "credential_revoked", "凭证已被撤销")
    if payload["p"] not in purposes:
        raise ApiError(403, "wrong_purpose", f"该令牌用途为 {payload['p']}，不能调用此接口")
    if t["config_revision"] is not None:
        from . import runtime
        settings = runtime.settings()
        if not settings["enabled"] or settings["revision"] != t["config_revision"] or runtime.scope_hash(t["room_id"]) != t["scope_hash"]:
            raise ApiError(409, "lease_not_held", "配置或会话访问范围已改变")
    agent = chat.agent_member_id(t["room_id"])
    if agent != cred["user_id"]:
        raise ApiError(403, "agent_not_in_room", "Agent 已不在该会话中")
    return t, cred, agent


# ---------------------------------------------------------------- 状态与心跳
def heartbeat(cred, info: dict) -> None:
    db.set_kv("hb_at", db.now())
    db.set_kv("hb_info", db.jl({k: str(v)[:200] for k, v in (info or {}).items() if k in ("adapter", "bridge_version", "guide_version", "host", "last_error")}))
    db.set_kv("hb_credential", str(cred["id"]))


def agent_status(viewer_id: int | None = None) -> dict:
    c = config.get()
    has_cred = bool(db.one("SELECT 1 FROM credentials WHERE revoked_at IS NULL"))
    hb = db.get_kv("hb_at")
    if not has_cred:
        state = "unconfigured"
    elif not hb or hb < db.ago(c.agent_offline_after):
        state = "offline"
    else:
        state = "online"
    info = db.jd(db.get_kv("hb_info"), {})
    if not c.allow_legacy_agent:
        from . import runtime
        s = runtime.settings()
        if not s["configured"]:
            state = "unconfigured"
        elif not s["enabled"]:
            state = "paused"
        elif state == "online" and info.get("adapter") != "claude-sdk":
            state = "offline"
        elif state == "online":
            error = db.jd(db.get_kv("last_error"), {})
            if error and error.get("at", "") > (db.get_kv("last_success_at") or ""):
                state = "degraded"
    backlog_chat = db.one("SELECT COUNT(*) AS c FROM agent_tasks WHERE kind='chat' AND status IN ('queued','leased')")["c"]
    backlog_events = 0
    for r in db.all("SELECT id FROM rooms WHERE consolidation_enabled=1"):
        backlog_events += pending_event_count(r["id"])
    result = {
        "state": state,
        "last_heartbeat": hb,
        "last_reply_at": db.get_kv("last_reply_at"),
        "last_consolidation_at": db.get_kv("last_consolidation_at"),
        "backlog_chat": backlog_chat,
        "backlog_events": backlog_events,
        "last_error": db.jd(db.get_kv("last_error")),
        "adapter": info.get("adapter"),
        "bridge_version": info.get("bridge_version"),
        "guide_version_agent": info.get("guide_version"),
        "guide_version_current": GUIDE_VERSION,
        "platform_version": PLATFORM_VERSION,
    }
    if viewer_id is not None and auth.get_user(viewer_id)["role"] != "owner":
        visible = {r["room_id"] for r in db.all("SELECT room_id FROM room_members WHERE user_id=? AND left_at IS NULL", (viewer_id,))}
        error = result["last_error"]
        target = db.one("SELECT room_id FROM agent_tasks WHERE id=?", (error.get("task_id"),)) if error else None
        if not target or target["room_id"] not in visible:
            result["last_error"] = None
        result["backlog_chat"] = sum(db.one("SELECT COUNT(*) c FROM agent_tasks WHERE room_id=? AND kind='chat' AND status IN ('queued','leased')", (rid,))["c"] for rid in visible)
        result["backlog_events"] = sum(pending_event_count(rid) for rid in visible)
    return result


_last_state: str | None = None


def broadcast_status_if_changed() -> None:
    global _last_state
    s = agent_status()
    if s["state"] != _last_state:
        _last_state = s["state"]
        ids = [r["id"] for r in db.all("SELECT id FROM users WHERE role!='agent' AND disabled=0")]
        for user_id in ids:
            hub.publish([user_id], {"type": "agent.status", "status": agent_status(user_id)})
