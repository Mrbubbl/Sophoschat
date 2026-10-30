"""面向外部 Agent 的接口。

两类凭证，职责分离（PRD §2.1）：
- 应用凭证 aca_…（Bridge 控制层持有，模型不可见）：心跳、领取/续租/完成任务。
- 任务令牌 tt.…（交给执行上下文）：只能访问“这个任务所在的 Room”，用途限定为 chat 或 consolidate，
  每次调用都回查数据库（租约、凭证、成员关系），Room 由令牌决定，不接受参数改写。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth, chat, db, hub, memory, tasks
from .errors import ApiError

router = APIRouter()


# ---------------------------------------------------------------- 应用凭证（控制层）
async def agent_cred(request: Request):
    h = request.headers.get("authorization", "")
    token = h[7:].strip() if h.lower().startswith("bearer ") else ""
    found = auth.credential_user(token)
    if not found:
        raise ApiError(401, "invalid_credential", "Agent 凭证无效或已撤销")
    return found  # (user, credential)


class HeartbeatIn(BaseModel):
    adapter: str | None = None
    bridge_version: str | None = None
    guide_version: str | None = None
    host: str | None = None
    last_error: str | None = None


class FailIn(BaseModel):
    error: str = "error"
    retry: bool = True


class CompleteIn(BaseModel):
    result_message_id: int | None = None


@router.post("/api/agent/heartbeat")
async def heartbeat(body: HeartbeatIn, found=Depends(agent_cred)):
    _, cred = found
    tasks.heartbeat(cred, body.model_dump(exclude_none=True))
    tasks.broadcast_status_if_changed()
    return {"ok": True, "guide_version": tasks.GUIDE_VERSION, "platform_version": tasks.PLATFORM_VERSION, "server_time": db.now()}


@router.post("/api/agent/lease")
async def lease(wait: int = 0, owner: str = "", found=Depends(agent_cred)):
    """领取下一个任务；wait>0 时长轮询（最多 30 秒），有新任务立即返回。"""
    _, cred = found
    wait = max(0, min(int(wait), 30))
    r = tasks.lease_next(cred, owner)
    if r or not wait:
        return {"lease": r}
    if await hub.wait_for_work(wait):
        r = tasks.lease_next(cred, owner)
    return {"lease": r}


@router.post("/api/agent/tasks/{task_id}/renew")
async def renew(task_id: int, found=Depends(agent_cred)):
    return tasks.renew(task_id, found[1])


@router.post("/api/agent/tasks/{task_id}/complete")
async def complete(task_id: int, body: CompleteIn, found=Depends(agent_cred)):
    return {"task": tasks.complete(task_id, found[1], body.result_message_id)}


@router.post("/api/agent/tasks/{task_id}/fail")
async def fail(task_id: int, body: FailIn, found=Depends(agent_cred)):
    return {"task": tasks.fail(task_id, found[1], body.error, body.retry)}


# ---------------------------------------------------------------- 任务令牌（工具层）
def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else request.headers.get("x-task-token", "")


class Ctx:
    def __init__(self, task, cred, agent_id):
        self.task, self.cred, self.agent_id = task, cred, agent_id
        self.room_id = task["room_id"]
        self.purpose = task["kind"]


def tool_ctx(*purposes: str):
    allowed = purposes or ("chat", "consolidate")

    async def dep(request: Request) -> Ctx:
        t, cred, agent = tasks.verify_tool_token(_bearer(request), allowed)
        return Ctx(t, cred, agent)

    return dep


any_ctx = tool_ctx("chat", "consolidate")
chat_ctx = tool_ctx("chat")
consolidate_ctx = tool_ctx("consolidate")


TOOLS = [
    {"name": "whoami", "method": "GET", "path": "/api/tools/whoami", "purposes": ["chat", "consolidate"], "desc": "当前任务、Room、成员"},
    {"name": "messages.list", "method": "GET", "path": "/api/tools/messages", "purposes": ["chat", "consolidate"], "desc": "分页/搜索/定位读取本 Room 的原文"},
    {"name": "messages.thread", "method": "GET", "path": "/api/tools/messages/{id}/thread", "purposes": ["chat", "consolidate"], "desc": "回复链与附近原文"},
    {"name": "attachments.get", "method": "GET", "path": "/api/tools/attachments/{id}", "purposes": ["chat"], "desc": "读取本 Room 的图片/语音原文件"},
    {"name": "attachments.transcript", "method": "PUT", "path": "/api/tools/attachments/{id}/transcript", "purposes": ["chat"], "desc": "写回语音转写文字"},
    {"name": "messages.send", "method": "POST", "path": "/api/tools/messages", "purposes": ["chat"], "desc": "以 Agent 身份发言，带幂等键"},
    {"name": "topics.list", "method": "GET", "path": "/api/tools/topics", "purposes": ["chat", "consolidate"], "desc": "列出/检索稳定主题"},
    {"name": "topics.get", "method": "GET", "path": "/api/tools/topics/{id}", "purposes": ["chat", "consolidate"], "desc": "主题详情、条目、变化历史"},
    {"name": "memory.search", "method": "GET", "path": "/api/tools/memory/search", "purposes": ["chat", "consolidate"], "desc": "在本 Room 内检索记忆条目"},
    {"name": "batch.get", "method": "GET", "path": "/api/tools/batch", "purposes": ["consolidate"], "desc": "读取本次整理批次（事件、原文、待重算条目）"},
    {"name": "sources.verify", "method": "POST", "path": "/api/tools/sources/verify", "purposes": ["chat", "consolidate"], "desc": "按消息 ID 与版本核对来源是否仍有效"},
    {"name": "memory.commit", "method": "POST", "path": "/api/tools/memory/commit", "purposes": ["consolidate"], "desc": "原子提交主题/条目/回顾变更并推进游标"},
    {"name": "disclosure.*", "method": "POST", "path": "/api/tools/disclosure/request", "purposes": [], "desc": "P1 授权披露：当前未启用"},
]


@router.get("/api/tools/capabilities")
async def capabilities(ctx: Ctx = Depends(any_ctx)):
    from . import memory as m

    return {
        "platform_version": tasks.PLATFORM_VERSION,
        "guide_version": tasks.GUIDE_VERSION,
        "purpose": ctx.purpose,
        "tools": [t for t in TOOLS if ctx.purpose in t["purposes"] or not t["purposes"]],
        "not_enabled": ["disclosure.request", "disclosure.approve", "disclosure.send"],
        "enums": {
            "item_type": sorted(m.ITEM_TYPES),
            "nature": sorted(m.NATURES),
            "state": sorted(m.ITEM_STATES),
            "adoption": sorted(m.ADOPTIONS),
            "ops": ["topic.create", "topic.update", "topic.merge", "item.create", "item.update", "item.retire"],
        },
        "openapi": "/openapi.json",
    }


@router.get("/api/tools/whoami")
async def whoami(ctx: Ctx = Depends(any_ctx)):
    room = db.one("SELECT * FROM rooms WHERE id=?", (ctx.room_id,))
    t = db.one("SELECT * FROM agent_tasks WHERE id=?", (ctx.task["id"],))
    params = db.jd(t["params"], {})
    trigger = None
    if t["trigger_message_id"]:
        m = db.one("SELECT * FROM messages WHERE id=?", (t["trigger_message_id"],))
        trigger = chat.ser_message(m) if m else None
    return {
        "task": tasks.ser_task(t) | {"params": params},
        "purpose": ctx.purpose,
        "agent_user_id": ctx.agent_id,
        "room": {"id": room["id"], "type": room["type"], "title": room["title"]},
        "members": [dict(id=m["id"], username=m["username"], display_name=m["display_name"], role=m["role"]) for m in chat.member_rows(ctx.room_id)],
        "trigger_message": trigger,
        "guide_version": tasks.GUIDE_VERSION,
    }


@router.get("/api/tools/messages")
async def tool_messages(before_seq: int | None = None, after_seq: int | None = None, around_seq: int | None = None, limit: int = 50, q: str | None = None, sender_id: int | None = None, ctx: Ctx = Depends(any_ctx)):
    return chat.get_messages(ctx.room_id, ctx.agent_id, before_seq=before_seq, after_seq=after_seq, around_seq=around_seq, limit=min(limit, 100), q=q, sender_id=sender_id)


@router.get("/api/tools/messages/{message_id}/thread")
async def tool_thread(message_id: int, ctx: Ctx = Depends(any_ctx)):
    hist = db.one("SELECT history_from_seq FROM room_members WHERE room_id=? AND user_id=?", (ctx.room_id, ctx.agent_id))["history_from_seq"]
    m = db.one("SELECT * FROM messages WHERE id=? AND room_id=? AND seq>?", (message_id, ctx.room_id, hist))
    if not m:
        raise ApiError(404, "message_not_found", "消息不在当前会话")
    chain, cur, hops = [m], m, 0
    while cur["reply_to"] and hops < 20:
        cur = db.one("SELECT * FROM messages WHERE id=? AND room_id=? AND seq>?", (cur["reply_to"], ctx.room_id, hist))
        if not cur:
            break
        chain.append(cur)
        hops += 1
    near = chat.get_messages(ctx.room_id, ctx.agent_id, around_seq=m["seq"], limit=11)["messages"]
    return {"chain": chat.ser_messages(list(reversed(chain))), "nearby": near}


class SendIn(BaseModel):
    body: str
    reply_to: int | None = None
    idempotency_key: str | None = None


@router.post("/api/tools/messages")
async def tool_send(body: SendIn, ctx: Ctx = Depends(chat_ctx)):
    t = ctx.task
    m = db.one("SELECT deleted_at FROM messages WHERE id=?", (t["trigger_message_id"],))
    if not m or m["deleted_at"]:
        raise ApiError(409, "source_deleted", "触发消息已删除，不应再回复")
    key = (body.idempotency_key or f"task-{t['id']}")[:64]
    with db.tx():
        msg, dup = chat.send_message(ctx.room_id, ctx.agent_id, body.body, key, body.reply_to or t["trigger_message_id"])
        db.run("UPDATE agent_tasks SET result_message_id=? WHERE id=?", (msg["id"], t["id"]))
    return {"message": msg, "duplicate": dup}


MAX_TOOL_ATT = 25 * 1024 * 1024


def _tool_attachment(att_id: int, ctx: Ctx):
    from . import config as _cfg

    a = db.one("SELECT * FROM attachments WHERE id=? AND room_id=? AND deleted_at IS NULL AND message_id IS NOT NULL", (att_id, ctx.room_id))
    if not a:
        raise ApiError(404, "attachment_not_found")
    m = db.one("SELECT seq, deleted_at FROM messages WHERE id=?", (a["message_id"],))
    hist = db.one("SELECT history_from_seq FROM room_members WHERE room_id=? AND user_id=?", (ctx.room_id, ctx.agent_id))
    if not m or m["deleted_at"] or (hist and m["seq"] <= hist["history_from_seq"]):
        raise ApiError(404, "attachment_not_found")
    if a["mime"] not in chat.IMAGE_MIMES and a["mime"] not in chat.AUDIO_MIMES:
        raise ApiError(415, "unsupported_type", "Agent 只能读取图片和语音附件")
    if a["size"] > MAX_TOOL_ATT:
        raise ApiError(413, "too_large")
    p = _cfg.get().data_dir / a["path"]
    if not p.exists():
        raise ApiError(404, "attachment_missing")
    return a, p


@router.get("/api/tools/attachments/{att_id}")
async def tool_attachment(att_id: int, ctx: Ctx = Depends(chat_ctx)):
    """读取本 Room 里的图片/语音原文件（仅聊天任务；任务令牌限定在当前 Room）。"""
    a, p = _tool_attachment(att_id, ctx)
    return FileResponse(p, media_type=a["mime"], headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


class TranscriptIn(BaseModel):
    text: str


@router.put("/api/tools/attachments/{att_id}/transcript")
async def tool_transcript(att_id: int, body: TranscriptIn, ctx: Ctx = Depends(chat_ctx)):
    return chat.set_transcript(att_id, ctx.room_id, body.text)


@router.get("/api/tools/topics")
async def tool_topics(q: str | None = None, status: str | None = "active", ctx: Ctx = Depends(any_ctx)):
    return {"topics": memory.list_topics(ctx.room_id, status or None, q), "unassigned_items": memory.pending_items(ctx.room_id)}


@router.get("/api/tools/topics/{topic_id}")
async def tool_topic(topic_id: int, history: int = 0, ctx: Ctx = Depends(any_ctx)):
    return {"topic": memory.get_topic(topic_id, ctx.room_id, bool(history))}


@router.get("/api/tools/memory/search")
async def tool_memory_search(q: str, limit: int = 10, ctx: Ctx = Depends(any_ctx)):
    return memory.search_memory(ctx.room_id, q, min(limit, 30))


@router.get("/api/tools/batch")
async def tool_batch(ctx: Ctx = Depends(consolidate_ctx)):
    return memory.build_batch(ctx.task)


class SourcesIn(BaseModel):
    sources: list[dict] = Field(default_factory=list)


@router.post("/api/tools/sources/verify")
async def tool_verify(body: SourcesIn, ctx: Ctx = Depends(any_ctx)):
    return {"results": memory.verify_sources(ctx.room_id, body.sources)}


class CommitIn(BaseModel):
    idempotency_key: str | None = None
    event_to: int | None = None
    ops: list[dict] = Field(default_factory=list)
    summaries: list[dict] = Field(default_factory=list)


@router.post("/api/tools/memory/commit")
async def tool_commit(body: CommitIn, ctx: Ctx = Depends(consolidate_ctx)):
    return memory.commit_batch(ctx.task, ctx.agent_id, body.model_dump())


@router.post("/api/tools/disclosure/request")
async def disclosure_not_enabled(ctx: Ctx = Depends(any_ctx)):
    raise ApiError(501, "not_enabled", "跨会话授权披露（P1）当前未启用，Agent 不得自行模拟批准流程")
