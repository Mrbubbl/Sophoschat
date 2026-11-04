"""Local ASR queue. SQLite owns job state; only a subprocess performs inference.

No audio, model loading or blocking inference runs on the application's event loop.
The model is reused between jobs and released after five minutes without work.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import signal
import sys
import time

from . import chat, config, db, hub
from .errors import ApiError

log = logging.getLogger("aichat.asr")
ERRORS = {
    "asr_disabled": "服务器已关闭语音转文字",
    "asr_not_installed": "服务器尚未安装本地语音识别依赖，请联系 Owner",
    "asr_model_missing": "服务器尚未安装语音模型，请联系 Owner",
    "asr_ffmpeg_missing": "服务器缺少 FFmpeg，请联系 Owner",
    "asr_busy": "转写队列已满，请稍后重试",
    "asr_timeout": "转写超时，请重试或发送较短的语音",
    "asr_invalid_audio": "语音文件无法解码",
    "asr_too_long": "语音超过服务器的转写时长限制",
    "asr_no_speech": "未识别到清晰语音，请重试",
    "asr_failed": "语音识别失败，请稍后重试",
}


def availability() -> dict:
    c = config.get()
    error = None
    if not c.asr_enabled:
        error = "asr_disabled"
    elif any(importlib.util.find_spec(p) is None for p in ("sherpa_onnx", "numpy")):
        error = "asr_not_installed"
    elif not all((Path(c.asr_model_dir) / p).is_file() for p in ("model.int8.onnx", "tokens.txt")):
        error = "asr_model_missing"
    elif not shutil.which(c.asr_ffmpeg):
        error = "asr_ffmpeg_missing"
    return {"enabled": c.asr_enabled, "ready": error is None, "engine": "sensevoice-small-int8",
            "auto_agent": c.asr_auto_agent, "max_seconds": c.asr_max_seconds,
            "error": error, "message": ERRORS.get(error)}


def notify(att_id: int) -> None:
    a = db.one("SELECT message_id,room_id FROM attachments WHERE id=? AND deleted_at IS NULL", (att_id,))
    if a and a["message_id"]:
        m = db.one("SELECT * FROM messages WHERE id=? AND deleted_at IS NULL", (a["message_id"],))
        if m:
            chat._notify(a["room_id"], {"type": "message.updated", "message": chat.ser_message(m)})


def result(a) -> dict:
    error = a["transcription_error"]
    return {"attachment_id": a["id"], "message_id": a["message_id"],
            "status": "done" if a["transcript"] else a["transcription_state"] or "idle",
            "text": a["transcript"], "error": error, "message": ERRORS.get(error),
            "engine": a["transcription_engine"] or ("legacy" if a["transcript"] else "sensevoice-small-int8")}


def enqueue(a, *, strict: bool = True) -> dict:
    if a["transcript"] or a["transcription_state"] in ("queued", "running"):
        return result(a)
    error = availability()["error"]
    if not error and db.one("SELECT COUNT(*) n FROM attachments WHERE transcription_state IN ('queued','running') AND deleted_at IS NULL")["n"] >= 32:
        error = "asr_busy"
    if error and strict:
        raise ApiError(429 if error == "asr_busy" else 503, error, ERRORS[error])
    db.run("UPDATE attachments SET transcription_state=?,transcription_error=?,transcription_generation=?,transcription_updated_at=? WHERE id=?",
           ("failed" if error else "queued", error, secrets.token_hex(16), db.now(), a["id"]))
    db.on_commit(lambda: notify(a["id"]))
    return result(db.one("SELECT * FROM attachments WHERE id=?", (a["id"],)))


def for_message(message_id: int, user_id: int, *, request: bool = False) -> dict:
    with db.tx():
        m = db.one("SELECT * FROM messages WHERE id=? AND deleted_at IS NULL", (message_id,))
        if not m:
            raise ApiError(404, "message_not_found")
        room = chat.require_member(m["room_id"], user_id)
        if m["seq"] <= room["history_from_seq"]:
            raise ApiError(404, "message_not_found")
        a = db.one("SELECT * FROM attachments WHERE id=? AND deleted_at IS NULL", (m["attachment_id"],))
        if not a or a["mime"] not in chat.AUDIO_MIMES or a["duration_ms"] is None:
            raise ApiError(400, "not_voice", "这不是语音消息")
        return enqueue(a) if request else result(a)


def prepare_agent(msg, agent_id: int) -> list[int]:
    """Only auto-transcribe the triggering voice and its visible quoted voice.

Do not scan other rooms or retroactively transcribe the entire chat history.
"""
    if not config.get().asr_auto_agent:
        return []
    ids = []
    hist = chat.require_member(msg["room_id"], agent_id)["history_from_seq"]
    for mid in (msg["id"], msg["reply_to"]):
        if not mid:
            continue
        m = db.one("SELECT * FROM messages WHERE id=? AND room_id=? AND deleted_at IS NULL AND seq>?", (mid, msg["room_id"], hist))
        a = db.one("SELECT * FROM attachments WHERE id=? AND deleted_at IS NULL", (m["attachment_id"],)) if m else None
        if a and a["mime"] in chat.AUDIO_MIMES and a["duration_ms"] is not None:
            enqueue(a, strict=False)
            ids.append(a["id"])
    return ids


def pending_for_task(t) -> bool:
    params = db.jd(t["params"], {})
    ids = params.get("voice_attachments", [])
    if t["kind"] == "consolidate":
        if params.get("mode", "pending") == "pending":
            ids = [r["attachment_id"] for r in db.all("SELECT DISTINCT m.attachment_id FROM messages m JOIN events e ON e.message_id=m.id WHERE m.room_id=? AND e.id>? AND e.id<=?", (t["room_id"], params.get("event_from", 0), params.get("event_to", 0)))]
        else:
            ids = [r["attachment_id"] for r in db.all("SELECT attachment_id FROM messages WHERE room_id=? AND seq>=? AND seq<=?", (t["room_id"], params.get("from_seq", 0), params.get("to_seq", 0)))]
    return any(db.one("SELECT 1 FROM attachments WHERE id=? AND room_id=? AND deleted_at IS NULL AND transcription_state IN ('queued','running')", (aid, t["room_id"])) for aid in ids)


class Worker:
    def __init__(self):
        self.process = None

    async def close(self):
        p, self.process = self.process, None
        if p and p.returncode is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await p.wait()

    async def recognize(self, a) -> dict:
        c = config.get()
        if not self.process or self.process.returncode is not None:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "aichat.asr_worker", "--parent", str(os.getpid()),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                cwd=str(Path(__file__).resolve().parent.parent), limit=256 * 1024)
        p = self.process
        job = {"path": str(c.data_dir / a["path"]), "model_dir": c.asr_model_dir,
               "ffmpeg": c.asr_ffmpeg, "threads": c.asr_threads, "max_seconds": c.asr_max_seconds}
        try:
            p.stdin.write((json.dumps(job) + "\n").encode())
            await p.stdin.drain()
            line = await asyncio.wait_for(p.stdout.readline(), c.asr_timeout)
            return json.loads(line)
        except asyncio.CancelledError:
            await self.close()
            raise
        except asyncio.TimeoutError:
            await self.close()
            return {"error": "asr_timeout"}
        except (OSError, ValueError, BrokenPipeError):
            await self.close()
            return {"error": "asr_failed"}


async def run_one(worker: Worker) -> bool:
    a = db.one("SELECT a.* FROM attachments a JOIN messages m ON m.id=a.message_id WHERE a.transcription_state='queued' AND a.deleted_at IS NULL AND m.deleted_at IS NULL ORDER BY a.transcription_updated_at,a.id LIMIT 1")
    if not a:
        return False
    generation = a["transcription_generation"]
    db.run("UPDATE attachments SET transcription_state='running',transcription_updated_at=? WHERE id=?", (db.now(), a["id"]))
    notify(a["id"])
    output = await worker.recognize(a)
    with db.tx():
        current = db.one("SELECT * FROM attachments WHERE id=? AND deleted_at IS NULL AND transcription_generation=? AND transcription_state='running'", (a["id"], generation))
        m = db.one("SELECT * FROM messages WHERE id=? AND deleted_at IS NULL", (a["message_id"],))
        if not current or not m:
            return True
        text = (output.get("text") or "").strip()[:20000]
        error = output.get("error") or (None if text else "asr_no_speech")
        if error:
            error = error if error in ERRORS else "asr_failed"
            db.run("UPDATE attachments SET transcription_state='failed',transcription_error=?,transcription_updated_at=? WHERE id=?", (error, db.now(), a["id"]))
            db.on_commit(lambda: notify(a["id"]))
        else:
            chat.set_transcript(a["id"], a["room_id"], text, engine="sensevoice-small-int8")
        db.on_commit(hub.wake_agent)
    return True


async def serve():
    # An interrupted inference has no result; safely retry it after restart.
    db.run("UPDATE attachments SET transcription_state='queued' WHERE transcription_state='running' AND deleted_at IS NULL")
    worker = Worker()
    last_work = time.monotonic()
    try:
        while True:
            try:
                if await run_one(worker):
                    last_work = time.monotonic()
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("local ASR job failed")
                await worker.close()
                with db.tx():
                    failed = db.all("SELECT id FROM attachments WHERE transcription_state='running'")
                    db.run("UPDATE attachments SET transcription_state='failed',transcription_error='asr_failed',transcription_updated_at=? WHERE transcription_state='running'", (db.now(),))
                    for a in failed:
                        db.on_commit(lambda aid=a["id"]: notify(aid))
                    db.on_commit(hub.wake_agent)
            if time.monotonic() - last_work > 300:
                await worker.close()
            await asyncio.sleep(0.5)
    finally:
        await worker.close()
