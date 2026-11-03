from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import artifacts, chat, db, runtime
from .agent_api import Ctx, chat_ctx
from .api import current_user, uid
from .errors import ApiError

router = APIRouter()


class PublishIn(BaseModel):
    filename: str = Field(max_length=160)
    title: str = Field(default="", max_length=160)
    content: str = Field(max_length=5 * 1024 * 1024)
    change_summary: str = Field(default="", max_length=500)
    slot: str = "main"
    browser_check: dict | None = None


class DeliveryIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=20000)
    base_version_id: int | None = None


@router.post("/api/tools/deliveries")
async def start(body: DeliveryIn, ctx: Ctx = Depends(chat_ctx)):
    return {"task": artifacts.start_delivery(ctx.task, body.instruction, body.base_version_id)}


@router.post("/api/tools/artifacts/publish")
async def publish(body: PublishIn, ctx: Ctx = Depends(chat_ctx)):
    return {"artifact_version": artifacts.publish(ctx.task, ctx.agent_id, body.model_dump())}


@router.get("/api/tools/artifacts/{version_id}")
async def tool_read(version_id: int, ctx: Ctx = Depends(chat_ctx)):
    v = artifacts.authorized(version_id, ctx.agent_id)
    if v["room_id"] != ctx.room_id:
        raise ApiError(404, "artifact_not_found")
    text = artifacts.file_path(v).read_text(encoding="utf-8")
    runtime.receipt(ctx.task, "artifact.read", str(version_id))
    runtime.receipt(ctx.task, "artifact.verified", str(version_id))
    return {"artifact_version": artifacts.serialize(v), "content": text}


@router.get("/api/tools/artifacts")
async def tool_list(ctx: Ctx = Depends(chat_ctx)):
    return {"versions": artifacts.task_versions(ctx.task)}


@router.get("/api/rooms/{room_id}/artifacts")
async def room_files(room_id: int, u=Depends(current_user)):
    member = chat.require_member(room_id, uid(u))
    rows = db.all("SELECT v.id FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id JOIN messages m ON m.id=v.message_id WHERE a.room_id=? AND a.deleted_at IS NULL AND m.deleted_at IS NULL AND m.seq>? AND v.is_test=0 ORDER BY v.id DESC LIMIT 100", (room_id, member["history_from_seq"]))
    return {"versions": [artifacts.serialize(artifacts.version_row(r["id"])) for r in rows]}


@router.get("/api/artifact-versions/{version_id}")
async def get_version(version_id: int, u=Depends(current_user)):
    v = artifacts.authorized(version_id, uid(u))
    versions = db.all("SELECT id FROM artifact_versions WHERE artifact_id=? ORDER BY number DESC", (v["artifact_id"],))
    visible = []
    for row in versions:
        try:
            visible.append(artifacts.serialize(artifacts.authorized(row["id"], uid(u))))
        except ApiError:
            continue
    return {"artifact_version": artifacts.serialize(v), "versions": visible}


@router.get("/api/artifact-versions/{version_id}/content")
async def content(version_id: int, u=Depends(current_user)):
    v = artifacts.authorized(version_id, uid(u))
    return PlainTextResponse(artifacts.file_path(v).read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@router.get("/api/artifact-versions/{version_id}/download")
async def download(version_id: int, u=Depends(current_user)):
    v = artifacts.authorized(version_id, uid(u))
    return FileResponse(artifacts.file_path(v), media_type=v["mime"], filename=v["filename"], content_disposition_type="attachment", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


PREVIEW_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; frame-src 'none'; worker-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts"


@router.get("/api/artifact-versions/{version_id}/preview")
async def preview(version_id: int, u=Depends(current_user)):
    v = artifacts.authorized(version_id, uid(u))
    if v["kind"] != "html":
        raise ApiError(415, "not_html")
    return FileResponse(artifacts.file_path(v), media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store", "Content-Security-Policy": PREVIEW_CSP, "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
