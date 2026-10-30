"""Agent Bridge：一个逻辑常驻任务，领取聊天互动与后台整理任务，按 Room 隔离执行。

控制层（本文件）持有应用凭证 aca_…；交给适配器/模型的只有当前任务的上下文，
工具调用使用只绑定“这个 Room + 这个用途”的任务令牌。任务之间不共享任何状态。
"""
from __future__ import annotations

import base64
import datetime
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import notion
from .adapters import Adapter
from .client import BridgeApiError, Http

BRIDGE_VERSION = "0.1.0"
log = logging.getLogger("aichat.bridge")


class LeaseKeeper:
    """工作较久时自动续租，避免任务被回收后重复处理。"""

    def __init__(self, http: Http, cred: str, task_id: int, every: float):
        self.http, self.cred, self.task_id, self.every = http, cred, task_id, every
        self._stop = threading.Event()
        self.lost = False
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                self.http.call("POST", f"/api/agent/tasks/{self.task_id}/renew", self.cred)
            except BridgeApiError as e:
                if e.abandon:
                    self.lost = True
                    return
            except Exception:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()


class Bridge:
    def __init__(self, base_url: str, credential: str, adapter: Adapter, owner: str = "bridge", guide_path: str | None = None, lease_every: float = 30):
        self.http = Http(base_url)
        self.cred = credential
        self.adapter = adapter
        self.owner = owner
        self.guide_path = guide_path
        self.lease_every = lease_every
        self.guide_version: str | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ 控制层
    def heartbeat(self) -> dict:
        info = {"adapter": self.adapter.name, "bridge_version": BRIDGE_VERSION, "guide_version": self.guide_version, "host": self.owner}
        if self.last_error:
            info["last_error"] = self.last_error
        r = self.http.call("POST", "/api/agent/heartbeat", self.cred, info)
        if r.get("guide_version") != self.guide_version:
            self.sync_guide(r.get("guide_version"))
            info["guide_version"] = self.guide_version  # 让服务器立刻看到已同步的版本
            r = self.http.call("POST", "/api/agent/heartbeat", self.cred, info)
        return r

    def sync_guide(self, version: str | None = None) -> None:
        """下载服务器上唯一可信的 AGENT_GUIDE.md；版本变化时更新本地副本。"""
        try:
            text = self.http.call("GET", "/agent-guide.md")
            if self.guide_path:
                Path(self.guide_path).write_text(text, encoding="utf-8")
            self.guide_version = version or self.guide_version
            log.info("已同步 AGENT_GUIDE（版本 %s）", self.guide_version)
        except Exception as e:  # 同步失败不影响工作
            log.warning("同步 AGENT_GUIDE 失败：%s", e)

    def lease(self, wait: int = 0) -> dict | None:
        r = self.http.call("POST", "/api/agent/lease", self.cred, params={"wait": wait, "owner": self.owner}, timeout=wait + 30)
        return r.get("lease")

    def run_once(self, wait: int = 0) -> bool:
        lease = self.lease(wait)
        if not lease:
            return False
        self.process(lease)
        return True

    def run_forever(self) -> None:
        def hb_loop():
            while not self._stop.is_set():
                try:
                    self.heartbeat()
                except Exception as e:
                    log.warning("心跳失败：%s", e)
                self._stop.wait(float(os.environ.get('AICHAT_HEARTBEAT_SECONDS', '20')))

        threading.Thread(target=hb_loop, daemon=True).start()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if self.run_once(wait=25):
                    backoff = 1.0
            except BridgeApiError as e:
                if e.status in (401, 403):
                    log.error("凭证无效或已撤销（%s），Bridge 退出", e.code)
                    return
                log.warning("领取任务失败：%s；%.0fs 后重试", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as e:
                log.exception("意外错误：%s", e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------ 任务处理
    def process(self, lease: dict) -> None:
        task, token = lease["task"], lease["task_token"]
        tid = task["id"]
        try:
            with LeaseKeeper(self.http, self.cred, tid, self.lease_every) as keeper:
                if lease["purpose"] == "chat":
                    result_id = self._chat(tid, token)
                else:
                    result_id = self._consolidate(tid, task, token)
                if keeper.lost:
                    log.warning("任务 %s 的租约已丢失，放弃提交", tid)
                    return
            self.http.call("POST", f"/api/agent/tasks/{tid}/complete", self.cred, {"result_message_id": result_id})
            self.last_error = None
        except BridgeApiError as e:
            if e.abandon:
                log.warning("任务 %s 已不属于本 Bridge（%s），放弃", tid, e.code)
                return
            self._fail(tid, f"{e.code}: {e.message}")
        except Exception as e:
            log.exception("任务 %s 失败", tid)
            self._fail(tid, f"{type(e).__name__}: {e}")

    def _fail(self, tid: int, error: str) -> None:
        self.last_error = error[:200]
        try:
            self.http.call("POST", f"/api/agent/tasks/{tid}/fail", self.cred, {"error": error[:400], "retry": True})
        except Exception as e:
            log.warning("上报失败也失败了：%s", e)

    def _tool(self, method: str, path: str, token: str, **kw):
        return self.http.call(method, "/api/tools" + path, token, **kw)

    # ---- 聊天互动
    def _chat(self, tid: int, token: str) -> int | None:
        who = self._tool("GET", "/whoami", token)
        trigger = who["trigger_message"]
        if not trigger or trigger.get("deleted"):
            raise BridgeApiError(409, "source_deleted", "触发消息已删除")
        params = (who.get("task") or {}).get("params") or {}
        if params.get("intent") == "transcribe":
            return self._transcribe(token, trigger)
        body = trigger.get("body") or ""
        summary = bool(re.search(r"/总结", body))
        limit = 100 if summary else 30
        history = self._tool("GET", "/messages", token, params={"before_seq": trigger["seq"] + 1, "limit": limit})["messages"]
        if summary and len(history) >= limit:  # 纪要要看得更远：再往前取一页
            older = self._tool("GET", "/messages", token, params={"before_seq": history[0]["seq"], "limit": 100})["messages"]
            history = older + history
        query = body[:200]
        memory = self._tool("GET", "/memory/search", token, params={"q": query, "limit": 8}) if query.strip() else {"items": [], "topics": []}
        ctx = {
            "agent_name": next((m["display_name"] for m in who["members"] if m["id"] == who["agent_user_id"]), "Agent"),
            "room": who["room"],
            "members": who["members"],
            "trigger": trigger,
            "history": history,
            "memory": memory,
            "images": self._images(token, trigger, history),
        }
        if summary:
            ctx["command"] = "summary"
        reply = self.adapter.chat(ctx).strip()
        if not reply:
            raise RuntimeError("模型返回了空回复")
        if summary and re.search(r"/总结存档|存档", body):
            reply += self._archive(reply) if notion.enabled() else "\n\n（还没有配置 Notion，纪要只保存在这个聊天里；可以长按这条消息收藏。）"
        sent = self._tool("POST", "/messages", token, body={"body": reply, "reply_to": trigger["id"], "idempotency_key": f"task-{tid}"})
        return sent["message"]["id"]

    def _images(self, token: str, trigger: dict, history: list[dict]) -> list[dict]:
        """最近的图片（触发消息优先，最多 3 张，单张不超过 6MB）一并交给模型。"""
        cands = [m for m in [trigger] + list(reversed(history)) if (m.get("attachment") or {}).get("is_image") and not m.get("deleted")]
        out: list[dict] = []
        seen = set()
        for m in cands:
            a = m["attachment"]
            if a["id"] in seen or a.get("size", 0) > 6 * 1024 * 1024:
                continue
            seen.add(a["id"])
            try:
                data, ctype = self.http.get_bytes(f"/api/tools/attachments/{a['id']}", token)
            except BridgeApiError as e:
                log.warning("图片 %s 读取失败：%s", a["id"], e)
                continue
            out.append({"id": m["id"], "mime": a.get("mime") or ctype.split(";")[0], "b64": base64.b64encode(data).decode()})
            if len(out) >= 3:
                break
        return out

    def _transcribe(self, token: str, trigger: dict) -> None:
        a = trigger.get("attachment")
        if not a or not a.get("is_voice"):
            raise RuntimeError("这条消息不是语音")
        data, _ = self.http.get_bytes(f"/api/tools/attachments/{a['id']}", token)
        text = self.adapter.transcribe(data, a["filename"], a["mime"]).strip()
        if not text:
            text = "（没有识别到内容）"
        self._tool("PUT", f"/attachments/{a['id']}/transcript", token, body={"text": text})
        return None

    def _archive(self, reply: str) -> str:
        """纪要存到 Notion；失败不影响聊天里的回复。"""
        first = reply.splitlines()[0] if reply else ""
        title = re.sub(r"^#+\s*", "", first).strip() or "讨论纪要"
        title = f"{title}（{datetime.date.today().isoformat()}）"
        try:
            url = notion.create_page(title, reply)
            return f"\n\n已存档到 Notion：{url}" if url else "\n\n已存档到 Notion。"
        except Exception as e:
            log.warning("存档到 Notion 失败：%s", e)
            return f"\n\n（存档到 Notion 失败：{str(e)[:120]}）"

    # ---- 后台整理
    def _consolidate(self, tid: int, task: dict, token: str) -> None:
        err: str | None = None
        for attempt in range(3):
            batch = self._tool("GET", "/batch", token)
            pending = batch["mode"] == "pending"
            if not batch["messages"]:
                plan = {"summaries": [], "ops": []}
            else:
                topics = self._tool("GET", "/topics", token)["topics"]
                text = " ".join((m.get("body") or "") for m in batch["messages"])[:300]
                items = self._tool("GET", "/memory/search", token, params={"q": text, "limit": 20})["items"] if text.strip() else []
                ctx = {"batch": batch, "topics": topics, "items": items, "previous_error": err}
                plan = self.adapter.consolidate(ctx)
            payload = {
                "idempotency_key": f"consolidate-{tid}",
                "event_to": batch["event_to"] if pending else None,
                "ops": plan.get("ops") or [],
                "summaries": plan.get("summaries") or [],
            }
            try:
                self._tool("POST", "/memory/commit", token, body=payload)
                return None
            except BridgeApiError as e:
                if e.abandon:
                    raise
                if e.code in ("stale_source", "version_conflict", "topic_exists", "invalid_op", "invalid_source", "sources_required", "manual_correction_locked", "adoption_needs_human_source", "author_not_in_sources", "invalid_content", "invalid_type", "invalid_nature", "invalid_author_kind", "unknown_op", "unknown_ref", "tombstoned", "invalid_state", "invalid_adoption", "reason_required", "invalid_author", "author_kind_mismatch", "topic_not_found", "item_not_found", "adoption_not_applicable", "invalid_payload"):
                    err = f"错误码 {e.code}：{e.message} {e.extra}"
                    log.warning("提交被拒（第 %d 次）：%s", attempt + 1, err)
                    continue
                raise
        raise RuntimeError(f"整理提交连续被服务器拒绝：{err}")
