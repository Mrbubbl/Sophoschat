"""每个用户的外观与播放偏好（跨设备同步）。只接受白名单内的值。"""
from __future__ import annotations

import json
import re

from . import chat, db
from .errors import ApiError

ACCENTS = {"indigo", "teal", "blue", "violet", "rose", "orange", "green", "slate"}
FONTS = {"s", "m", "l", "xl"}
UIS = {"nothing", "classic"}
RATES = {1.0, 1.25, 1.5, 2.0}
DEFAULTS = {"ui": "nothing", "accent": "teal", "font": "m", "bg": None, "dim": True, "voice_rate": 1.0, "autoplay": True, "collapse_long": True, "link_preview": True, "motion": True}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _load(u) -> dict:
    raw = u["prefs"] if "prefs" in u.keys() else None
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        d = {}
    return {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}


def get(u) -> dict:
    d = _load(u)
    v = u["bg_image"] if "bg_image" in u.keys() else None
    d["bg_image_url"] = f"/api/me/background?v={v}" if v else None
    return d


def _check(key: str, value):
    if key == "accent":
        if value in ACCENTS or (isinstance(value, str) and _HEX.match(value)):
            return value
    elif key == "font":
        if value in FONTS:
            return value
    elif key == "ui":
        if value in UIS:
            return value
    elif key == "bg":
        return chat.valid_bg(value)
    elif key in ("dim", "autoplay", "collapse_long", "link_preview", "motion"):
        if isinstance(value, bool):
            return value
    elif key == "voice_rate":
        if isinstance(value, (int, float)) and float(value) in RATES:
            return float(value)
    raise ApiError(400, "invalid_pref", f"不支持的设置：{key}")


def save(user_id: int, patch: dict) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ApiError(400, "invalid_pref")
    unknown = set(patch) - set(DEFAULTS)
    if unknown:
        raise ApiError(400, "invalid_pref", "不支持的设置：" + "、".join(sorted(unknown)))
    u = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    cur = _load(u)
    for k, v in patch.items():
        cur[k] = _check(k, v)
    db.run("UPDATE users SET prefs=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), user_id))
    return get(db.one("SELECT * FROM users WHERE id=?", (user_id,)))
