"""Agent 适配器：把“聊天回复”和“整理计划”交给某个具体的模型/Agent。

平台数据协议不依赖任何一个适配器；换适配器只需要换这里。
- mock       离线演示/测试用的规则实现（不是真正的智能）
- openai     任何 OpenAI 兼容的 /chat/completions 服务（含多数国内模型服务）
- anthropic  Anthropic Messages API
- command    把提示词写进任意命令的标准输入、读取标准输出（例如 codex exec / claude -p 的无交互模式）
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Callable
import logging

log = logging.getLogger('aichat.bridge')

from . import prompts, search


class Adapter:
    name = "base"

    def complete(self, system: str, user: str, *, workdir: str | None = None) -> str:  # pragma: no cover
        raise NotImplementedError

    def transcribe(self, data: bytes, filename: str, mime: str) -> str:
        raise RuntimeError("当前适配器不支持语音转文字（需要 openai 适配器与支持 /audio/transcriptions 的服务）")

    def chat(self, ctx: dict) -> str:
        sys = prompts.CHAT_SYSTEM.format(agent_name=ctx.get("agent_name", "Agent"))
        return self.complete(sys, prompts.chat_user(ctx), workdir=ctx.get("workdir")).strip()

    def consolidate(self, ctx: dict) -> dict:
        out = self.complete(prompts.CONSOLIDATE_SYSTEM, prompts.consolidate_user(ctx), workdir=ctx.get("workdir"))
        return prompts.extract_json(out)


# ------------------------------------------------------------------ HTTP 类
def _post_json(url: str, headers: dict, body: dict, timeout: int = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "aichat-bridge/0.2", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型服务返回 {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接模型服务：{e.reason}") from None


def _user_content(text: str, images: list[dict] | None, responses: bool = False):
    """有图片时，用户消息变成“文字 + 图片”的内容列表（图片以 data URI 内嵌）。"""
    if not images:
        return text
    if responses:
        return [{"type": "input_text", "text": text}] + [{"type": "input_image", "image_url": f"data:{i['mime']};base64,{i['b64']}"} for i in images]
    return [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": f"data:{i['mime']};base64,{i['b64']}"}} for i in images]


def _multipart(fields: dict, file_field: str, filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    import uuid

    b = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    out += f'--{b}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + f"\r\n--{b}--\r\n".encode()
    return out, f"multipart/form-data; boundary={b}"


class OpenAICompat(Adapter):
    name = "openai"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None, temperature: float = 0.2):
        self.base = (base_url or os.environ.get("AICHAT_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.key = api_key or os.environ.get("AICHAT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("AICHAT_LLM_MODEL")
        self.temperature = temperature
        if not self.key or not self.model:
            raise SystemExit("openai 适配器需要 AICHAT_LLM_API_KEY 与 AICHAT_LLM_MODEL（可选 AICHAT_LLM_BASE_URL）")

    def _chat_native_search(self, ctx):
        """走 /responses 接口，用服务商自带的联网搜索（AICHAT_NATIVE_SEARCH=1）。搜索由对方服务器完成，不需要另外的搜索 Key。"""
        import datetime

        sys = prompts.CHAT_SYSTEM.format(agent_name=ctx.get("agent_name", "Agent")) + prompts.SEARCH_ADDENDUM.format(today=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
        d = _post_json(
            f"{self.base}/responses",
            {"Authorization": f"Bearer {self.key}"},
            {"model": self.model, "instructions": sys, "tools": [{"type": "web_search"}],
             "input": [{"role": "user", "content": _user_content(prompts.chat_user(ctx), ctx.get("images") if not getattr(self, "_no_images", False) else None, responses=True)}] if ctx.get("images") and not getattr(self, "_no_images", False) else prompts.chat_user(ctx)},
            timeout=240,
        )
        if d.get("error"):
            raise RuntimeError(f"模型服务返回错误：{str(d['error'])[:200]}")
        texts, links = [], []
        for o in d.get("output", []):
            if o.get("type") == "web_search_call":
                log.info("联网搜索（服务商内置）")
            if o.get("type") == "message":
                for c in o.get("content", []):
                    if c.get("type") in ("output_text", "text") and c.get("text"):
                        texts.append(c["text"])
                    for a in c.get("annotations") or []:
                        if a.get("type") == "url_citation" and a.get("url") and a["url"] not in links:
                            links.append(a["url"])
        out = "\n".join(texts).strip()
        if not out:
            raise RuntimeError("模型服务没有返回文字")
        if links and not any(u in out for u in links):
            out += "\n\n来源：\n" + "\n".join(links[:3])
        return out

    def chat(self, ctx):
        """聊天回复：配置了搜索服务时，让模型可以自己决定调用 web_search（最多 3 轮），整理任务不联网。"""
        if os.environ.get("AICHAT_NATIVE_SEARCH") == "1" and not getattr(self, "_no_native", False):
            try:
                return self._chat_native_search(ctx)
            except RuntimeError as e:
                if "400" in str(e) or "404" in str(e) or "422" in str(e):
                    self._no_native = True  # 服务不支持 /responses 或内置搜索：以后走普通路径
                    log.warning("内置联网搜索不可用，退回：%s", str(e)[:120])
                else:
                    raise
        sys = prompts.CHAT_SYSTEM.format(agent_name=ctx.get("agent_name", "Agent"))
        imgs = None if getattr(self, "_no_images", False) else ctx.get("images")
        if not search.enabled() or getattr(self, "_no_tools", False):
            try:
                return self.complete(sys, prompts.chat_user(ctx), workdir=ctx.get("workdir"), images=imgs).strip()
            except RuntimeError as e:
                if imgs and ("400" in str(e) or "422" in str(e)):
                    self._no_images = True  # 这个模型/服务不接受图片：以后只发文字
                    log.warning("模型服务拒绝了图片，退回纯文字：%s", str(e)[:120])
                    return self.complete(sys, prompts.chat_user(ctx), workdir=ctx.get("workdir")).strip()
                raise
        import datetime

        sys += prompts.SEARCH_ADDENDUM.format(today=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"))
        msgs = [{"role": "system", "content": sys}, {"role": "user", "content": _user_content(prompts.chat_user(ctx), imgs)}]
        for step in range(4):
            body = {"model": self.model, "temperature": self.temperature, "messages": msgs}
            if step < 3:
                body["tools"] = [search.TOOL_SPEC]
            try:
                d = _post_json(f"{self.base}/chat/completions", {"Authorization": f"Bearer {self.key}"}, body)
            except RuntimeError as e:
                if step == 0 and ("400" in str(e) or "422" in str(e)) and "tools" in body:
                    self._no_tools = True  # 这个服务不支持函数调用：以后不再带工具，退回普通回复
                    return self.complete(sys, prompts.chat_user(ctx), workdir=ctx.get("workdir")).strip()
                raise
            msg = d["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for c in calls:
                try:
                    q = json.loads(c["function"].get("arguments") or "{}").get("query", "")
                except Exception:
                    q = ""
                log.info("联网搜索：%s", q[:80])
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": search.run_tool(q)})
        return (msg.get("content") or "").strip()

    def complete(self, system, user, *, workdir=None, images=None):
        d = _post_json(
            f"{self.base}/chat/completions",
            {"Authorization": f"Bearer {self.key}"},
            {"model": self.model, "temperature": self.temperature, "messages": [{"role": "system", "content": system}, {"role": "user", "content": _user_content(user, images)}]},
        )
        return d["choices"][0]["message"]["content"]

    def transcribe(self, data, filename, mime):
        """语音转文字：OpenAI 兼容的 /audio/transcriptions。可单独配置另一个服务：AICHAT_STT_BASE_URL / AICHAT_STT_API_KEY / AICHAT_STT_MODEL（默认 whisper-1）。"""
        body, ctype = _multipart({"model": os.environ.get("AICHAT_STT_MODEL", "whisper-1")}, "file", filename, mime, data)
        req = urllib.request.Request(f"{os.environ.get('AICHAT_STT_BASE_URL') or self.base}/audio/transcriptions", data=body, method="POST", headers={"Content-Type": ctype, "Authorization": f"Bearer {os.environ.get('AICHAT_STT_API_KEY') or self.key}", "User-Agent": "aichat-bridge/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return (json.loads(r.read()).get("text") or "").strip()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"语音识别服务返回 {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接语音识别服务：{e.reason}") from None


class AnthropicAdapter(Adapter):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None, base_url: str | None = None):
        self.key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("AICHAT_LLM_API_KEY")
        self.model = model or os.environ.get("AICHAT_LLM_MODEL")
        self.base = (base_url or os.environ.get("AICHAT_LLM_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        if not self.key or not self.model:
            raise SystemExit("anthropic 适配器需要 ANTHROPIC_API_KEY 与 AICHAT_LLM_MODEL")

    def complete(self, system, user, *, workdir=None):
        d = _post_json(
            f"{self.base}/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": user}]},
        )
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")


class CommandAdapter(Adapter):
    """每次调用在全新的临时目录里启动命令：不同任务/Room 之间不共享工作目录与缓存。"""

    name = "command"

    def __init__(self, command: str | None = None, timeout: int = 600):
        self.command = command or os.environ.get("AICHAT_LLM_COMMAND")
        self.timeout = timeout
        if not self.command:
            raise SystemExit('command 适配器需要 --command 或 AICHAT_LLM_COMMAND，例如 "codex exec -" 或 "claude -p"')

    def complete(self, system, user, *, workdir=None):
        wd = tempfile.mkdtemp(prefix="aichat-task-")
        try:
            p = subprocess.run(shlex.split(self.command), input=f"{system}\n\n{user}", capture_output=True, text=True, timeout=self.timeout, cwd=wd)
        finally:
            shutil.rmtree(wd, ignore_errors=True)
        if p.returncode != 0:
            raise RuntimeError(f"命令退出码 {p.returncode}: {p.stderr[-300:]}")
        return p.stdout


# ------------------------------------------------------------------ 演示/测试
class CallbackAdapter(Adapter):
    """测试里用：直接给出回复与整理计划。"""

    name = "callback"

    def __init__(self, chat_fn: Callable[[dict], str] | None = None, consolidate_fn: Callable[[dict], dict] | None = None):
        self.chat_fn, self.consolidate_fn = chat_fn, consolidate_fn

    def chat(self, ctx):
        return self.chat_fn(ctx) if self.chat_fn else "ok"

    def consolidate(self, ctx):
        return self.consolidate_fn(ctx) if self.consolidate_fn else {"summaries": [], "ops": []}


_GREET = re.compile(r"^(你好|哈喽|嗨|hi|hello|hey|在吗|早|晚安|哈哈+|好的?|嗯+|ok|okay|收到|谢谢)[!！。.~ ]*$", re.I)
_ADOPT = re.compile(r"(那就|就这样|就定|决定|采纳|同意|行[，,]?就|听你的)")
_KEYWORDS = ["旅行", "买车", "预算", "电影", "AI", "项目"]


class MockAdapter(Adapter):
    """规则实现，仅用于离线演示与联调：不理解语义，只做关键词归类。真实使用请换成真正的模型。"""

    name = "mock"

    def chat(self, ctx):
        t = ctx["trigger"]
        text = re.sub(r"@\w+", "", t.get("body") or "").strip()
        mem = (ctx.get("memory") or {}).get("items", [])
        extra = ""
        if mem:
            first = mem[0]
            src = ",".join(f"#{s['message_id']}" for s in first["sources"]) or "手工"
            extra = f"\n本会话里和这个相关的记录：{first['content']}（来源：{src}）"
        return f"（演示 Agent）收到：{text[:80]}{extra}"

    def consolidate(self, ctx):
        b = ctx["batch"]
        msgs = [m for m in b["messages"] if not m["deleted"] and m["body"]]
        if not msgs:
            return {"summaries": [], "ops": []}
        topics = {t["title"]: t for t in ctx["topics"]}
        ops, summaries, refs, last_agent_item = [], [], {}, None
        for m in msgs:
            text = m["body"].strip()
            if _GREET.match(text) or len(text) < 6:
                continue
            kw = next((k for k in _KEYWORDS if k in text), None)
            title = f"{kw}相关" if kw else None
            topic_ref = None
            if title:
                if title in topics:
                    topic_ref = {"id": topics[title]["id"]}
                else:
                    if title not in refs:
                        refs[title] = f"t{len(refs)+1}"
                        ops.append({"op": "topic.create", "ref": refs[title], "title": title, "aliases": [kw], "overview": f"关于{kw}的讨论"})
                    topic_ref = {"ref": refs[title]}
            is_agent = m["kind"] == "agent"
            if m["kind"] == "human" and _ADOPT.search(text) and last_agent_item:
                ops.append({"op": "item.update", "item_id": last_agent_item["id"], "expected_version": last_agent_item["version"], "adoption": "adopted", "add_sources": [{"message_id": m["id"], "version": m["version"]}], "reason": "用户明确表示采纳"})
                continue
            ops.append(
                {
                    "op": "item.create",
                    "topic": topic_ref,
                    "type": "suggestion" if is_agent else "opinion",
                    "content": text[:300],
                    "author_kind": "agent" if is_agent else "human",
                    "author_user_id": m["sender_id"],
                    "nature": "agent_suggestion" if is_agent else "stated",
                    "adoption": "none" if is_agent else None,
                    "sources": [{"message_id": m["id"], "version": m["version"]}],
                    "reason": "演示规则提取",
                }
            )
        summaries.append({"period_start": msgs[0]["created_at"], "period_end": msgs[-1]["created_at"], "content": f"这段时间共 {len(msgs)} 条消息。", "sources": [{"message_id": msgs[0]["id"], "version": msgs[0]["version"]}]})
        return {"summaries": summaries, "ops": ops}


def make_adapter(name: str, **kw) -> Adapter:
    name = (name or "mock").lower()
    if name == "mock":
        return MockAdapter()
    if name == "openai":
        return OpenAICompat(kw.get("base_url"), kw.get("api_key"), kw.get("model"))
    if name == "anthropic":
        return AnthropicAdapter(kw.get("api_key"), kw.get("model"), kw.get("base_url"))
    if name == "command":
        return CommandAdapter(kw.get("command"))
    raise SystemExit(f"未知适配器：{name}（可选 mock / openai / anthropic / command）")
