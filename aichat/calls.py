"""一对一语音 / 视频通话的信令（WebRTC）。服务器只转发信令，不经手音视频数据；通话状态只在内存里。

信令种类：invite（呼叫）→ accept / decline / busy → offer / answer / ice → hangup / cancel
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from . import chat, hub, push
from .errors import ApiError

KINDS = {"invite", "accept", "decline", "busy", "cancel", "offer", "answer", "ice", "hangup"}
MAX_DATA = 24000
RING_SECONDS = 60
_calls: dict[str, dict] = {}


def ice_config(user_id: int) -> dict:
    """返回浏览器用的 ICE 服务器。配置了 TURN 密钥时发放有时效的临时账号（coturn use-auth-secret）。"""
    servers: list[dict] = []
    raw = os.environ.get("AICHAT_ICE_SERVERS")
    if raw:
        try:
            servers = list(json.loads(raw))
        except Exception:
            servers = []
    url, secret = os.environ.get("AICHAT_TURN_URL"), os.environ.get("AICHAT_TURN_SECRET")
    if url and secret:
        user = f"{int(time.time()) + 6 * 3600}:{user_id}"
        cred = base64.b64encode(hmac.new(secret.encode(), user.encode(), hashlib.sha1).digest()).decode()
        servers.append({"urls": [u.strip() for u in url.split(",") if u.strip()], "username": user, "credential": cred})
    if not servers:
        servers = [{"urls": "stun:stun.l.google.com:19302"}]
    return {"ice_servers": servers, "turn": bool(url and secret)}


def _purge() -> None:
    now = time.time()
    for k in [k for k, c in _calls.items() if now - c["t0"] > 4 * 3600 or (not c.get("accepted") and now - c["t0"] > RING_SECONDS + 30)]:
        _calls.pop(k, None)


def _log(c: dict, reason: str) -> None:
    """通话结束后在会话里留一条记录（由发起人发出，幂等）。"""
    kind = "视频通话" if c["media"] == "video" else "语音通话"
    if c.get("accepted"):
        s = int(time.time() - c["accepted"])
        body = f"[通话] {kind} · {s // 60}:{s % 60:02d}"
    elif reason == "decline":
        body = f"[通话] {kind}被对方拒绝"
    elif reason == "busy":
        body = f"[通话] 对方正忙，{kind}未接通"
    else:
        body = f"[通话] 未接通的{kind}"
    try:
        chat.send_message(c["room_id"], c["caller"], body, client_msg_id=f"call-{c['id']}")
    except ApiError:
        pass


def _to(user_id: int, payload: dict) -> None:
    hub.publish([user_id], payload)


def handle(user_id: int, d: dict) -> None:
    kind = d.get("kind")
    room_id = d.get("room_id")
    call_id = str(d.get("call_id") or "")[:64]
    if kind not in KINDS or not isinstance(room_id, int) or not call_id:
        return
    data = d.get("data")
    if data is not None and len(json.dumps(data)) > MAX_DATA:
        return
    try:
        room = chat.require_member(room_id, user_id)
    except ApiError:
        return
    humans = chat.human_member_ids(room_id)
    if room["type"] != "dm" or len(humans) != 2:
        _to(user_id, {"type": "call.signal", "kind": "unsupported", "call_id": call_id, "room_id": room_id})
        return
    peer = next(i for i in humans if i != user_id)
    _purge()
    media = "video" if d.get("media") == "video" else "audio"

    if kind == "invite":
        busy = any(c["room_id"] == room_id or user_id in (c["caller"], c["callee"]) or peer in (c["caller"], c["callee"]) for c in _calls.values())
        if busy:
            _to(user_id, {"type": "call.signal", "kind": "busy", "call_id": call_id, "room_id": room_id, "from": peer})
            return
        _calls[call_id] = {"id": call_id, "room_id": room_id, "caller": user_id, "callee": peer, "media": media, "t0": time.time(), "accepted": None}
        sender = chat.auth.get_user(user_id)
        push.notify_call_later(room_id, user_id, sender["display_name"], peer, media)
    else:
        c = _calls.get(call_id)
        if not c or c["room_id"] != room_id or user_id not in (c["caller"], c["callee"]):
            return
        peer = c["callee"] if user_id == c["caller"] else c["caller"]
        if kind == "accept" and user_id == c["callee"]:
            c["accepted"] = time.time()
        if kind in ("decline", "busy", "cancel", "hangup"):
            _calls.pop(call_id, None)
            _log(c, "decline" if kind == "decline" else "busy" if kind == "busy" else kind)
    _to(peer, {"type": "call.signal", "kind": kind, "call_id": call_id, "room_id": room_id, "from": user_id, "media": media, "data": data})


def on_disconnect(user_id: int) -> None:
    """用户掉线：结束他参与的所有通话，通知对方。"""
    for cid, c in list(_calls.items()):
        if user_id in (c["caller"], c["callee"]):
            _calls.pop(cid, None)
            _to(c["callee"] if user_id == c["caller"] else c["caller"], {"type": "call.signal", "kind": "hangup", "call_id": cid, "room_id": c["room_id"], "from": user_id})
            _log(c, "cancel")


def reset() -> None:
    _calls.clear()
