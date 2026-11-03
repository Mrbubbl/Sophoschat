"""Web Push 离线推送（VAPID）。

服务端把新消息通过浏览器厂商的推送服务发给已订阅的设备；载荷按标准端到端加密，推送服务看不到内容。
依赖 pywebpush；未安装时功能自动关闭，聊天本身不受影响。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os

from . import config, db

log = logging.getLogger("aichat.push")

try:  # 可选依赖
    from py_vapid import Vapid
    from pywebpush import WebPushException, webpush
    from cryptography.hazmat.primitives import serialization

    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False

_vapid = None


def _key_path():
    return config.get().data_dir / "vapid_private.pem"


def _load():
    global _vapid
    p = _key_path()
    if _vapid is not None and p.exists():
        return _vapid
    if p.exists():
        _vapid = Vapid.from_file(str(p))
    else:
        v = Vapid()
        v.generate_keys()
        v.save_key(str(p))
        os.chmod(p, 0o600)
        _vapid = v
    return _vapid


def public_key() -> str | None:
    if not AVAILABLE:
        return None
    raw = _load().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscribe(user_id: int, endpoint: str, p256dh: str, auth: str, user_agent: str = "") -> None:
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth:
        from .errors import ApiError

        raise ApiError(400, "invalid_subscription", "订阅信息无效")
    with db.tx():
        db.run(
            "INSERT INTO push_subscriptions(endpoint, user_id, p256dh, auth, user_agent, created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, user_agent=excluded.user_agent",
            (endpoint, user_id, p256dh, auth, (user_agent or "")[:200], db.now()),
        )


def unsubscribe(user_id: int, endpoint: str) -> None:
    db.run("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?", (endpoint, user_id))


def count_for(user_id: int) -> int:
    return db.one("SELECT COUNT(*) n FROM push_subscriptions WHERE user_id=?", (user_id,))["n"]


def _preview(msg: dict) -> str:
    if msg.get("body"):
        return msg["body"][:140]
    a = msg.get("attachment") or {}
    if a.get("is_voice"):
        return "[语音]"
    if a.get("is_image"):
        return "[图片]"
    return "[文件]" if a else ""


def build_payload(room: dict, sender_name: str, msg: dict) -> dict:
    is_dm = room["type"] != "group"
    title = sender_name if is_dm else f"{room.get('title') or '群聊'} · {sender_name}"
    return {"title": title, "body": _preview(msg), "room_id": room["id"], "message_id": msg["id"], "tag": f"room-{room['id']}"}


def _send_blocking(subs: list[dict], payload_json: str) -> list[str]:
    """在线程里发送；返回已失效（应删除）的 endpoint。"""
    dead = []
    v = _load()
    for s in subs:
        try:
            webpush(
                subscription_info={"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                data=payload_json,
                vapid_private_key=v,
                vapid_claims={"sub": os.environ.get("AICHAT_VAPID_SUBJECT", "mailto:admin@example.com")},
                ttl=3600,
                timeout=10,
            )
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                dead.append(s["endpoint"])
            else:
                log.warning("push failed (%s): %s", code, str(e)[:200])
        except Exception as e:  # 网络等
            log.warning("push error: %s", str(e)[:200])
    return dead


async def notify_message(room: dict, sender_id: int, sender_name: str, recipient_ids: list[int], msg: dict) -> None:
    if not AVAILABLE or not recipient_ids:
        return
    import json

    q = ",".join("?" * len(recipient_ids))
    muted = {r["user_id"] for r in db.all(f"SELECT user_id FROM room_members WHERE room_id=? AND muted=1 AND user_id IN ({q})", [room["id"]] + recipient_ids)}
    targets = [i for i in recipient_ids if i != sender_id and i not in muted]
    if not targets:
        return
    from . import chat

    q = ",".join("?" * len(targets))
    rows = [dict(r) for r in db.all(f"SELECT user_id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id IN ({q})", targets)]
    if not rows:
        return
    base = build_payload(room, sender_name, msg)
    # 每个收件人一份：带上他自己的未读总数，用来设置应用图标角标
    batches = []
    for uid_ in sorted({r["user_id"] for r in rows}):
        batches.append(([r for r in rows if r["user_id"] == uid_], json.dumps({**base, "badge": chat.unread_total(uid_)}, ensure_ascii=False)))
    dead: list[str] = []
    for subs, payload in batches:
        dead += await asyncio.to_thread(_send_blocking, subs, payload)
    for ep in dead:
        db.run("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))


def reset() -> None:
    global _vapid
    _vapid = None


def notify_call_later(room_id: int, caller_id: int, caller_name: str, callee_id: int, media: str) -> None:
    """来电：给被叫的离线设备发一条推送（页面开着时由页面自己响铃）。"""
    if not AVAILABLE:
        return
    import json

    subs = [dict(r) for r in db.all("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=?", (callee_id,))]
    if not subs:
        return
    payload = json.dumps({"title": caller_name, "body": "来电：视频通话" if media == "video" else "来电：语音通话", "room_id": room_id, "tag": f"call-{room_id}", "call": True}, ensure_ascii=False)

    async def go():
        dead = await asyncio.to_thread(_send_blocking, subs, payload)
        for ep in dead:
            db.run("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))

    try:
        asyncio.get_running_loop().create_task(go())
    except RuntimeError:
        pass
