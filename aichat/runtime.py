"""Deterministic SDK control plane. No model calls or model-generated SQL here."""
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from . import auth, chat, config, db
from .errors import ApiError

SDK_VERSION = "0.2.176"
DEFAULTS = {
    "provider": "anthropic", "model": "", "base_url": "", "enabled": False,
    "memory_enabled": True, "max_concurrency": 2, "reserved_chat_slots": 1,
    "max_turns": 12, "timeout_seconds": 300, "max_run_usd": 0.25,
    "monthly_usd": 20.0, "user_monthly_usd": 10.0, "room_monthly_usd": 10.0,
    "max_file_bytes": 2 * 1024 * 1024, "memory_max_delay_seconds": 3600,
    "models": {}, "revision": 0,
    # 第二套接入（高级）：可选。交付文件等用途、或消息以 /pro 开头时使用；其余走上面的日常接入。
    "premium_model": "", "premium_base_url": "",
}


def _cipher() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.get().secret + b"server-agent").digest()))


def _encrypt(text: str) -> str:
    return _cipher().encrypt(text.encode()).decode()


def _decrypt(text: str | None) -> str:
    if not text:
        return ""
    try:
        return _cipher().decrypt(text.encode()).decode()
    except InvalidToken:
        raise ApiError(503, "agent_secret_unavailable", "服务端凭证无法解密，请 Owner 重新配置") from None


def settings(*, secrets: bool = False) -> dict:
    saved = db.jd(db.get_kv("server_agent_settings"), {})
    out = {**DEFAULTS, **saved}
    encrypted = out.pop("encrypted_api_key", None)
    premium_enc = out.pop("encrypted_premium_api_key", None)
    out["configured"] = bool(encrypted and out["model"])
    out["premium_configured"] = bool(premium_enc and out["premium_model"])
    if secrets:
        out["api_key"] = _decrypt(encrypted)
        out["premium_api_key"] = _decrypt(premium_enc)
    return out


def update_settings(changes: dict, actor_id: int) -> dict:
    if auth.get_user(actor_id)["role"] != "owner":
        raise ApiError(403, "owner_only")
    old = db.jd(db.get_kv("server_agent_settings"), {})
    values = {**DEFAULTS, **old}
    unknown = set(changes) - (set(DEFAULTS) - {"revision"}) - {"api_key", "premium_api_key"}
    if unknown:
        raise ApiError(422, "invalid_agent_settings", "未知设置字段")
    for k, value in changes.items():
        if k in ("api_key", "premium_api_key"):
            if value is not None:
                if not isinstance(value, str) or len(value) > 4096:
                    raise ApiError(422, "invalid_api_key")
                values["encrypted_api_key" if k == "api_key" else "encrypted_premium_api_key"] = _encrypt(value) if value else None
        else:
            values[k] = value
    if values["provider"] != "anthropic":
        raise ApiError(422, "unsupported_provider", "本版仅通过 Claude Agent SDK 接入 Anthropic；不回退到其他引擎")
    for k in ("model", "base_url", "premium_model", "premium_base_url"):
        if not isinstance(values[k], str) or len(values[k]) > 500 or "\n" in values[k]:
            raise ApiError(422, "invalid_agent_settings", k)
    for k in ("base_url", "premium_base_url"):
        url = urlparse(values[k])
        if values[k] and (url.username or url.password or url.query or url.fragment or not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")))):
            raise ApiError(422, "invalid_base_url", "模型网关需使用 HTTPS（本机测试地址除外）")
    if bool(values["premium_model"]) != bool(values.get("encrypted_premium_api_key")):
        raise ApiError(422, "invalid_agent_settings", "高级接入需要同时填写模型和 API Key（或都留空）")
    for k, lo, hi in (("max_concurrency", 1, 8), ("reserved_chat_slots", 0, 7), ("max_turns", 1, 100), ("timeout_seconds", 10, 3600), ("max_file_bytes", 1024, 5 * 1024 * 1024), ("memory_max_delay_seconds", 60, 86400)):
        if type(values[k]) is not int or not lo <= values[k] <= hi:
            raise ApiError(422, "invalid_agent_settings", k)
    if values["reserved_chat_slots"] >= values["max_concurrency"]:
        raise ApiError(422, "invalid_agent_settings", "交互预留必须小于总并发；单并发时设为 0")
    for k in ("max_run_usd", "monthly_usd", "user_monthly_usd", "room_monthly_usd"):
        if type(values[k]) not in (float, int) or not math.isfinite(values[k]) or values[k] <= 0 or values[k] > 100000:
            raise ApiError(422, "invalid_agent_settings", k)
    for k in ("enabled", "memory_enabled"):
        if type(values[k]) is not bool:
            raise ApiError(422, "invalid_agent_settings", k)
    if not isinstance(values["models"], dict) or set(values["models"]) - {"chat", "delivery", "memory"} or any(not isinstance(v, str) or not v or len(v) > 200 for v in values["models"].values()):
        raise ApiError(422, "invalid_agent_settings", "models")
    if values["enabled"] and not (values.get("encrypted_api_key") and values["model"]):
        raise ApiError(422, "agent_not_configured", "请先配置模型和服务端 API Key")
    values["revision"] = int(values["revision"]) + 1
    cancelled = db.all("SELECT id FROM agent_tasks WHERE status='leased' AND config_revision IS NOT NULL")
    with db.tx():
        db.set_kv("server_agent_settings", db.jl(values))
        # A changed key, model or permission cannot be used by an old worker.
        db.run("UPDATE agent_tasks SET status='cancelled', finished_at=?, error='agent_configuration_changed' WHERE status='leased' AND config_revision IS NOT NULL", (db.now(),))
        db.audit(f"user:{actor_id}", "agent.settings", None, revision=values["revision"])
    from . import tasks
    for row in cancelled:
        tasks._publish_task(row["id"])
    tasks._wake()
    return settings()


def service_credential() -> str:
    token = _decrypt(db.get_kv("server_agent_token"))
    if token and auth.credential_user(token):
        return token
    cid, token = auth.issue_credential("platform-sdk-service")
    with db.tx():
        db.set_kv("server_agent_credential_id", str(cid))
        db.set_kv("server_agent_token", _encrypt(token))
    return token


def require_service(cred) -> None:
    if str(cred["id"]) != db.get_kv("server_agent_credential_id"):
        raise ApiError(403, "platform_service_only", "此入口仅用于平台服务端 Agent")


def scope_hash(room_id: int) -> str:
    members = [dict(r) for r in db.all("SELECT user_id, joined_at, history_from_seq, left_at FROM room_members WHERE room_id=? ORDER BY user_id", (room_id,))]
    event = db.one("SELECT COALESCE(MAX(id),0) n FROM events WHERE room_id=? AND type IN ('member.changed','message.edited','message.deleted')", (room_id,))["n"]
    return hashlib.sha256(db.jl([members, event]).encode()).hexdigest()[:24]


def profile(t) -> str:
    return "memory" if t["kind"] == "consolidate" else t["profile"]


def _month_start() -> str:
    return db.now()[:7] + "-01T00:00:00.000Z"


def _spent(where: str = "", args: tuple = ()) -> float:
    return db.one("SELECT COALESCE(SUM(COALESCE(cost_usd, CASE WHEN finished_at IS NULL THEN reserved_usd ELSE 0 END)),0) n FROM task_attempts WHERE started_at>=?" + where, (_month_start(), *args))["n"]


def tier_for(t, s: dict) -> str:
    """main=日常接入；pro=高级接入。交付文件、或任务带 tier=pro（消息以 /pro 开头）时用高级，且仅在已配置时。"""
    if not s.get("premium_configured"):
        return "main"
    if t["profile"] == "delivery" or db.jd(t["params"], {}).get("tier") == "pro":
        return "pro"
    return "main"


def admission(t, s: dict) -> dict | None:
    from . import tasks
    p = profile(t)
    active = db.all("SELECT id, profile, session_key FROM agent_tasks WHERE status='leased' AND lease_until>? AND config_revision IS NOT NULL", (db.now(),))
    if len(active) >= s["max_concurrency"]:
        return None
    if p != "chat" and sum(r["profile"] != "chat" for r in active) >= s["max_concurrency"] - s["reserved_chat_slots"]:
        return None
    sh = scope_hash(t["room_id"])
    # Delivery continues a specific artifact chain; no global 'latest session'.
    identity = "room" if p != "delivery" else f"run-{t['id']}"
    tier = tier_for(t, s)
    key = f"room-{t['room_id']}:{p}:{identity}:{sh}:{s['revision']}:{tasks.GUIDE_VERSION}" + (":pro" if tier == "pro" and p != "delivery" else "")
    if any(r["session_key"] == key for r in active):
        return None
    cap = float(s["max_run_usd"])
    limits = [s["monthly_usd"] - _spent(), s["room_monthly_usd"] - _spent(" AND room_id=?", (t["room_id"],))]
    if t["initiator_id"]:
        limits.append(s["user_monthly_usd"] - _spent(" AND initiator_id=?", (t["initiator_id"],)))
    if min(limits) < cap:
        raise ApiError(429, "agent_budget_exhausted", "本月额度不足；未知费用按已预留额度计算，普通聊天不受影响")
    return {"profile": p, "scope_hash": sh, "session_key": key, "config_revision": s["revision"], "tier": tier, "model": (s["premium_model"] if tier == "pro" else (s["models"].get(p) or s["model"])), "reserved_usd": cap}


def begin_attempt(t, admitted: dict, s: dict) -> None:
    safe = {k: v for k, v in s.items() if k not in ("api_key", "premium_api_key", "configured", "premium_configured")}
    safe["model"] = admitted["model"]
    safe["tier"] = admitted["tier"]
    if admitted["tier"] == "pro":
        safe["base_url"] = s["premium_base_url"]
    db.run("INSERT INTO task_attempts(task_id,attempt,generation,room_id,initiator_id,profile,model,config_json,reserved_usd,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (t["id"], t["attempts"], t["lease_generation"], t["room_id"], t["initiator_id"], admitted["profile"], admitted["model"], db.jl(safe), admitted["reserved_usd"], db.now()))
    db.run("INSERT INTO sdk_sessions(session_key,room_id,profile,scope_hash,config_revision,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(session_key) DO NOTHING",
           (admitted["session_key"], t["room_id"], admitted["profile"], admitted["scope_hash"], s["revision"], db.now()))


def attempt_config(t) -> dict:
    a = db.one("SELECT * FROM task_attempts WHERE task_id=? AND attempt=?", (t["id"], t["attempts"]))
    if not a:
        raise ApiError(409, "sdk_attempt_missing")
    s = settings(secrets=True)
    if s["revision"] != t["config_revision"] or not (s["configured"] and s["enabled"]):
        raise ApiError(409, "agent_configuration_changed")
    session = db.one("SELECT * FROM sdk_sessions WHERE session_key=?", (t["session_key"],))
    from . import integrations
    cfg = db.jd(a["config_json"], {})
    return {"config": {**cfg, "api_key": s["premium_api_key"] if cfg.get("tier") == "pro" else s["api_key"], "cli_path": cli_path(), "integrations":integrations.configuration(secrets=True)}, "session": dict(session)}


def receipt(t, action: str, object_id: str = "") -> None:
    db.run("INSERT OR IGNORE INTO task_tool_receipts(task_id,attempt,action,object_id,created_at) VALUES(?,?,?,?,?)", (t["id"], t["attempts"], action, str(object_id), db.now()))


def record_result(t, result: dict) -> None:
    version = result.get("sdk_version")
    if version != SDK_VERSION:
        raise ApiError(409, "sdk_version_mismatch", f"执行服务需安装固定 SDK {SDK_VERSION}")
    cost = result.get("cost_usd")
    if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
        raise ApiError(422, "invalid_usage")
    usage = result.get("usage")
    if usage is not None and (not isinstance(usage, dict) or any(type(v) is not int or v < 0 for k, v in usage.items() if k in ("input_tokens", "output_tokens"))):
        raise ApiError(422, "invalid_usage")
    sid = result.get("session_id")
    if sid and (not isinstance(sid, str) or len(sid) > 100):
        raise ApiError(422, "invalid_session_id")
    with db.tx():
        a = db.one("SELECT * FROM task_attempts WHERE task_id=? AND attempt=?", (t["id"], t["attempts"]))
        if a["sdk_version"]:
            return  # a retried result receipt must not double-charge a call
        db.run("UPDATE task_attempts SET sdk_version=?, cost_usd=?, usage_json=?, finished_at=?, outcome=? WHERE task_id=? AND attempt=?",
               (version, cost, db.jl(usage) if usage is not None else None, db.now(), result.get("outcome", "completed")[:40], t["id"], t["attempts"]))
        if sid:
            db.run("UPDATE sdk_sessions SET sdk_session_id=?, total_cost=?, usage_json=?, updated_at=? WHERE session_key=?", (sid, result.get("session_total_cost"), db.jl(result.get("session_usage")), db.now(), t["session_key"]))
        receipt(t, "sdk.result")


def usage_for(user_id: int) -> dict:
    owner = auth.get_user(user_id)["role"] == "owner"
    scope = " WHERE a.started_at>=?" + ("" if owner else " AND EXISTS(SELECT 1 FROM room_members m WHERE m.room_id=a.room_id AND m.user_id=? AND m.left_at IS NULL)")
    args = (_month_start(),) if owner else (_month_start(), user_id)
    rows = db.all("SELECT a.* FROM task_attempts a" + scope + " ORDER BY a.started_at DESC LIMIT 200", args)
    total = db.one("SELECT COALESCE(SUM(a.cost_usd),0) cost,COALESCE(SUM(a.cost_usd IS NULL),0) unknown FROM task_attempts a" + scope, args)
    return {"attempts": [{k: r[k] for k in ("task_id", "attempt", "room_id", "profile", "model", "sdk_version", "reserved_usd", "cost_usd", "started_at", "finished_at", "outcome")} | {"usage": db.jd(r["usage_json"])} for r in rows],
            "known_cost_usd": total["cost"], "unknown_attempts": total["unknown"], "period": _month_start()[:7]}


def installed_sdk() -> dict:
    try:
        version = importlib.metadata.version("claude-agent-sdk")
        return {"installed": True, "version": version, "expected": SDK_VERSION, "runtime_available": bool(cli_path())}
    except (ImportError, importlib.metadata.PackageNotFoundError):
        return {"installed": False, "version": None, "expected": SDK_VERSION, "runtime_available": False}


def cli_path() -> str | None:
    override = os.environ.get("AICHAT_CLAUDE_CLI_PATH")
    if override:
        return str(Path(override).resolve()) if Path(override).is_file() else None
    try:
        import claude_agent_sdk
        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
        if bundled.is_file():
            return str(bundled)
    except ImportError:
        pass
    return shutil.which("claude")


def validate_completion(t) -> None:
    a = db.one("SELECT * FROM task_attempts WHERE task_id=? AND attempt=?", (t["id"], t["attempts"]))
    if not a or a["sdk_version"] != SDK_VERSION or a["outcome"] not in ("completed", "no_model_needed", "recovered_without_model"):
        raise ApiError(409, "sdk_result_required", "执行尚未返回有效 SDK 回执")
    if t["profile"] == "chat":
        message = db.one("SELECT id FROM messages WHERE id=? AND room_id=? AND kind='agent' AND deleted_at IS NULL", (t["result_message_id"],t["room_id"]))
        if not message:
            raise ApiError(409, "reply_missing", "回复尚未保存，不能标记完成")
    if t["profile"] == "delivery":
        versions = db.all("SELECT id FROM artifact_versions WHERE task_id=?", (t["id"],))
        if not versions:
            raise ApiError(409, "delivery_missing", "任务没有实际交付文件，不能标记完成")
        if db.jd(t["params"], {}).get("selfcheck"):
            actions = {r["action"] for r in db.all("SELECT action FROM task_tool_receipts WHERE task_id=? AND attempt=?", (t["id"],t["attempts"]))}
            if a["outcome"] != "completed" or not {"sdk.result", "messages.read", "artifact.verified", "artifact.read"}.issubset(actions):
                raise ApiError(409, "selfcheck_incomplete")
