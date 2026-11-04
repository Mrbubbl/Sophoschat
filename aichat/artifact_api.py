from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
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


# 预览运行在没有 allow-same-origin 的沙箱里，浏览器会禁止访问 localStorage / sessionStorage（一访问就抛 SecurityError），
# 很多小游戏、工具页用它存最高分或设置，脚本在这里就中断，页面"跑不起来"。
# 预览时在页面最前面注入一小段垫片：访问被禁时换成内存版存储，页面能正常运行（刷新后不保留，下载后用浏览器打开则是真正的存储）。
STORAGE_SHIM = (
    "<script>(function(){function mk(){var d={};return{getItem:function(k){return Object.prototype.hasOwnProperty.call(d,k)?d[k]:null},"
    "setItem:function(k,v){d[k]=String(v)},removeItem:function(k){delete d[k]},clear:function(){d={}},"
    "key:function(i){return Object.keys(d)[i]||null},get length(){return Object.keys(d).length}}}"
    "['localStorage','sessionStorage'].forEach(function(n){try{void window[n].length}catch(e){try{Object.defineProperty(window,n,{value:mk(),configurable:true})}catch(_){}}})})();</script>"
)


def with_shim(html: str) -> str:
    import re

    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[: m.end()] + STORAGE_SHIM + html[m.end():]
    m = re.search(r"<html[^>]*>", html, re.I)
    if m:
        return html[: m.end()] + STORAGE_SHIM + html[m.end():]
    return STORAGE_SHIM + html


@router.get("/api/artifact-versions/{version_id}/preview")
async def preview(version_id: int, u=Depends(current_user)):
    v = artifacts.authorized(version_id, uid(u))
    if v["kind"] != "html":
        raise ApiError(415, "not_html")
    text = artifacts.file_path(v).read_text(encoding="utf-8", errors="replace")
    return HTMLResponse(with_shim(text), headers={"Cache-Control": "no-store", "Content-Security-Policy": PREVIEW_CSP, "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
