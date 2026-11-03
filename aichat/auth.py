"""账号、浏览器会话、Agent 应用凭证。"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from . import config, db, security
from .errors import ApiError

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,32}$")
_failures: dict[tuple[str, str], list[float]] = {}


def public_user(r) -> dict:
    return {
        "id": r["id"],
        "username": r["username"],
        "display_name": r["display_name"],
        "role": r["role"],
        "disabled": bool(r["disabled"]),
        "avatar_url": avatar_url(r),
        "can_add_users": bool(r["role"] == "owner" or ("can_add_users" in r.keys() and r["can_add_users"])),
    }


def avatar_url(r) -> str | None:
    v = r["avatar"] if "avatar" in r.keys() else None
    return f"/api/avatars/{r['id']}?v={v}" if v else None


def get_user(user_id: int):
    return db.one("SELECT * FROM users WHERE id=?", (user_id,))


def create_user(username: str, display_name: str | None, password: str | None, role: str = "user") -> int:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise ApiError(400, "invalid_username", "用户名只能包含字母、数字、下划线和短横线（2-32 位）")
    if role != "agent":
        if not password or len(password) < 8:
            raise ApiError(400, "weak_password", "密码至少 8 位")
    with db.tx():
        if db.one("SELECT 1 FROM users WHERE username=?", (username,)):
            raise ApiError(409, "username_taken", "用户名已存在")
        if role == "owner" and db.one("SELECT 1 FROM users WHERE role='owner'"):
            raise ApiError(409, "owner_exists", "Owner 已存在")
        uid = db.insert(
            "INSERT INTO users(username, display_name, password_hash, role, created_at) VALUES(?,?,?,?,?)",
            (username, (display_name or username).strip()[:40], security.hash_password(password) if password else None, role, db.now()),
        )
        db.audit("system", "user.create", None, user_id=uid, role=role)
    return uid


def ensure_agent_user() -> int:
    r = db.one("SELECT id FROM users WHERE role='agent' ORDER BY id LIMIT 1")
    if r:
        return r["id"]
    return create_user("agent", "Agent", None, role="agent")


def agent_user_id() -> int | None:
    r = db.one("SELECT id FROM users WHERE role='agent' ORDER BY id LIMIT 1")
    return r["id"] if r else None


def set_password(user_id: int, new_password: str) -> None:
    if len(new_password or "") < 8:
        raise ApiError(400, "weak_password", "密码至少 8 位")
    with db.tx():
        db.run("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(new_password), user_id))
        db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.audit(f"user:{user_id}", "user.password_change", None)


def set_disabled(user_id: int, disabled: bool) -> None:
    with db.tx():
        db.run("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        if disabled:
            db.run("DELETE FROM sessions WHERE user_id=?", (user_id,))
        db.audit("system", "user.disable" if disabled else "user.enable", None, user_id=user_id)


def authenticate(username: str, password: str, ip: str = "") -> dict:
    key = ((username or "").lower(), ip)
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < 300]
    if len(recent) >= 5:
        raise ApiError(429, "too_many_attempts", "尝试次数过多，请 5 分钟后再试")
    r = db.one("SELECT * FROM users WHERE username=?", ((username or "").strip(),))
    ok = bool(r) and r["role"] != "agent" and not r["disabled"] and security.verify_password(password or "", r["password_hash"])
    if not ok:
        recent.append(now)
        _failures[key] = recent
        raise ApiError(401, "bad_credentials", "用户名或密码错误")
    _failures.pop(key, None)
    return r


def create_session(user_id: int, user_agent: str = "", ip: str = "") -> str:
    token = security.new_token("aus")
    exp = (datetime.now(timezone.utc) + timedelta(days=config.get().session_days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with db.tx():
        db.run(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, user_agent, last_seen_at, ip) VALUES(?,?,?,?,?,?,?)",
            (security.token_hash(token), user_id, db.now(), exp, (user_agent or "")[:200], db.now(), (ip or "")[:64]),
        )
        db.run("DELETE FROM sessions WHERE expires_at < ?", (db.now(),))
    return token


def session_user(token: str | None):
    if not token:
        return None
    return db.one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0 AND u.role!='agent'",
        (security.token_hash(token), db.now()),
    )


def touch_session(token: str | None) -> None:
    """最近活动时间：5 分钟内不重复写库。"""
    if not token:
        return
    h = security.token_hash(token)
    r = db.one("SELECT last_seen_at FROM sessions WHERE token_hash=?", (h,))
    if r and (not r["last_seen_at"] or r["last_seen_at"] < _minutes_ago(5)):
        db.run("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (db.now(), h))


def _minutes_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def device_label(ua: str) -> str:
    ua = ua or ""
    low = ua.lower()
    if "iphone" in low or "ipad" in low:
        os_ = "iPhone / iPad"
    elif "android" in low:
        os_ = "Android"
    elif "windows" in low:
        os_ = "Windows"
    elif "mac os" in low or "macintosh" in low:
        os_ = "Mac"
    elif "linux" in low:
        os_ = "Linux"
    else:
        os_ = "未知设备"
    if "samsungbrowser" in low:
        br = "三星浏览器"
    elif "edg/" in low:
        br = "Edge"
    elif "firefox" in low:
        br = "Firefox"
    elif "chrome" in low or "crios" in low:
        br = "Chrome"
    elif "safari" in low:
        br = "Safari"
    else:
        br = "浏览器"
    return f"{br} · {os_}"


def list_sessions(user_id: int, current_token: str | None) -> list[dict]:
    cur = security.token_hash(current_token) if current_token else None
    rows = db.all("SELECT * FROM sessions WHERE user_id=? AND expires_at>? ORDER BY COALESCE(last_seen_at, created_at) DESC", (user_id, db.now()))
    return [
        {
            "id": r["token_hash"][:16],
            "device": device_label(r["user_agent"]),
            "created_at": r["created_at"],
            "last_seen_at": r["last_seen_at"] or r["created_at"],
            "ip": r["ip"] or "",
            "current": r["token_hash"] == cur,
        }
        for r in rows
    ]


def revoke_session(user_id: int, sid: str, current_token: str | None) -> list[str]:
    """退出指定设备，返回被撤销会话的 token_hash（用于关闭它们的 WebSocket）。"""
    cur = security.token_hash(current_token) if current_token else None
    rows = db.all("SELECT token_hash FROM sessions WHERE user_id=? AND substr(token_hash,1,16)=?", (user_id, sid))
    hashes = [r["token_hash"] for r in rows if r["token_hash"] != cur]
    for h in hashes:
        db.run("DELETE FROM sessions WHERE token_hash=?", (h,))
    return hashes


def revoke_other_sessions(user_id: int, current_token: str | None) -> list[str]:
    cur = security.token_hash(current_token) if current_token else ""
    rows = db.all("SELECT token_hash FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, cur))
    db.run("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, cur))
    return [r["token_hash"] for r in rows]


def delete_session(token: str | None) -> None:
    if token:
        db.run("DELETE FROM sessions WHERE token_hash=?", (security.token_hash(token),))


# ---- Agent 应用凭证 -------------------------------------------------------------
def issue_credential(label: str = "") -> tuple[int, str]:
    agent_id = ensure_agent_user()
    token = security.new_token("aca")
    with db.tx():
        cid = db.insert(
            "INSERT INTO credentials(user_id, token_hash, label, created_at) VALUES(?,?,?,?)",
            (agent_id, security.token_hash(token), (label or "")[:60], db.now()),
        )
        db.audit("owner", "credential.issue", None, credential_id=cid, label=label)
    return cid, token


def credential_user(token: str | None):
    """返回 (agent 用户行, 凭证行)；无效或已撤销返回 None。"""
    if not token or not token.startswith("aca_"):
        return None
    c = db.one("SELECT * FROM credentials WHERE token_hash=? AND revoked_at IS NULL", (security.token_hash(token),))
    if not c:
        return None
    u = db.one("SELECT * FROM users WHERE id=? AND role='agent' AND disabled=0", (c["user_id"],))
    return (u, c) if u else None


def revoke_credential(cred_id: int) -> bool:
    with db.tx():
        cur = db.run("UPDATE credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (db.now(), cred_id))
        if cur.rowcount:
            # 撤销立即生效：正在处理的任务回到队列，旧任务令牌随即失效
            db.run(
                "UPDATE agent_tasks SET status='queued', lease_owner=NULL, lease_credential_id=NULL, lease_until=NULL "
                "WHERE status='leased' AND lease_credential_id=?",
                (cred_id,),
            )
            db.audit("owner", "credential.revoke", None, credential_id=cred_id)
        return bool(cur.rowcount)


def list_credentials() -> list[dict]:
    rows = db.all("SELECT id, label, created_at, revoked_at FROM credentials ORDER BY id DESC")
    return [dict(r) for r in rows]
