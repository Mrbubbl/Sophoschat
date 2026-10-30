"""密码哈希、令牌、任务作用域令牌签名。只用标准库。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

_N, _R, _P = 2**14, 8, 1


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, _unb64(dk))
    except Exception:
        return False


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- 任务作用域令牌：只绑定“某个任务 + 某个 Room + 某个用途” -------------------
def sign_task_token(secret: bytes, task_id: int, room_id: int, purpose: str, ttl: int) -> str:
    payload = {"t": task_id, "r": room_id, "p": purpose, "x": int(time.time()) + ttl, "n": secrets.token_hex(4)}
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())
    return f"tt.{raw}.{sig}"


def verify_task_token(secret: bytes, token: str) -> dict | None:
    try:
        prefix, raw, sig = token.split(".")
        if prefix != "tt":
            return None
        good = _b64(hmac.new(secret, raw.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(good, sig):
            return None
        payload = json.loads(_unb64(raw))
        if payload["x"] < time.time():
            return None
        return payload
    except Exception:
        return None
