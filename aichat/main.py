"""应用工厂与后台调度器。"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import agent_api, api, auth, chat, config, db, hub, tasks
from .errors import ApiError

log = logging.getLogger("aichat")
STATIC = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


async def scheduler_loop() -> None:
    last_slow = 0.0
    while True:
        await asyncio.sleep(config.get().scheduler_tick)
        try:
            tasks.maintenance()
            tasks.broadcast_status_if_changed()
            if time.time() - last_slow > 3600:
                last_slow = time.time()
                chat.retention_sweep()
                chat.orphan_attachment_sweep()
        except Exception:  # 调度器不能因一次异常而退出
            log.exception("scheduler error")


def create_app(data_dir=None, scheduler: bool = True, **overrides) -> FastAPI:
    c = config.load(data_dir, **overrides)
    db.connect(c.db_path)
    hub.reset()
    auth._failures.clear()
    auth.ensure_agent_user()
    for r in db.all("SELECT id FROM users WHERE role!='agent' AND disabled=0"):
        chat.ensure_agent_dm(r["id"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(scheduler_loop()) if scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="SophosChat", version=tasks.PLATFORM_VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)  # 交互式文档依赖外部 CDN，与严格 CSP 冲突；机器可读的 /openapi.json 保留

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "no-cache")  # 每次向服务器校验（ETag）；否则浏览器会按启发式规则缓存旧脚本好几个小时
        if not request.url.path.startswith("/api/attachments/"):
            resp.headers.setdefault("Content-Security-Policy", CSP)
            resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    app.include_router(api.router)
    app.include_router(agent_api.router)

    nocache = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers=nocache)

    @app.get("/sw.js", include_in_schema=False)
    def sw():
        return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers=nocache)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json", headers=nocache)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
