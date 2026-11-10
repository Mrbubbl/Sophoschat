"""WebSocket 推送中心 + Agent 长轮询唤醒。单进程内存实现（双人/小群足够）。"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Iterable

log = logging.getLogger("aichat.hub")

_clients: dict[int, set] = defaultdict(set)
_waiters: list[asyncio.Future] = []


_ws_session: dict = {}
_native: set = set()  # 安卓 App 的原生后台连接（/ws?client=android）：只用来收系统通知，不渲染界面


async def connect(user_id: int, ws, session_hash: str | None = None, native: bool = False) -> None:
    _clients[user_id].add(ws)
    if native:
        _native.add(ws)
    if session_hash:
        _ws_session[ws] = session_hash


async def close_sessions(session_hashes: list[str]) -> None:
    """会话被撤销：通知并关闭这些会话的 WebSocket。"""
    targets = set(session_hashes)
    for ws, h in list(_ws_session.items()):
        if h in targets:
            try:
                await ws.send_json({"type": "session.revoked"})
                await ws.close(code=4401)
            except Exception:
                pass


async def send_native(user_id: int, payload: dict) -> int:
    """只发给该用户的原生 App 连接；返回送达的连接数。"""
    n = 0
    for ws in list(_clients.get(user_id, ())):
        if ws in _native:
            try:
                await ws.send_json(payload)
                n += 1
            except Exception:
                _clients[user_id].discard(ws)
    return n


def has_native(user_id: int) -> bool:
    return any(ws in _native for ws in _clients.get(user_id, ()))


def disconnect(user_id: int, ws) -> None:
    _native.discard(ws)
    _ws_session.pop(ws, None)
    _clients[user_id].discard(ws)
    if not _clients[user_id]:
        _clients.pop(user_id, None)


async def _deliver(user_ids: list[int], payload: dict) -> None:
    for uid in user_ids:
        for ws in list(_clients.get(uid, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                _clients[uid].discard(ws)


def publish(user_ids: Iterable[int], payload: dict[str, Any]) -> None:
    """在事件循环里异步投递；没有运行中的循环（例如 CLI）时忽略。"""
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_deliver(ids, payload))


def online_users() -> list[int]:
    return list(_clients.keys())


async def wait_for_work(timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _waiters.append(fut)
    try:
        await asyncio.wait_for(fut, timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        if fut in _waiters:
            _waiters.remove(fut)


def wake_agent() -> None:
    for fut in list(_waiters):
        if fut.done():
            continue
        try:
            fut.get_loop().call_soon_threadsafe(lambda f=fut: (not f.done()) and f.set_result(True))
        except RuntimeError:
            pass


def reset() -> None:
    _clients.clear()
    _native.clear()
    _ws_session.clear()
    _waiters.clear()
