"""把讨论纪要存成 Notion 页面（可选）。

在 bridge.env 里配置：
  AICHAT_NOTION_TOKEN=secret_…        # Notion「内部集成」的令牌
  AICHAT_NOTION_PARENT=<页面 ID>      # 纪要会建在这个页面下面；这个页面要先“分享给”该集成
没配置时，“总结并存档”只在聊天里给出纪要。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def enabled() -> bool:
    return bool(os.environ.get("AICHAT_NOTION_TOKEN") and os.environ.get("AICHAT_NOTION_PARENT"))


def _rich(text: str) -> list[dict]:
    # Notion 单段文字上限 2000 字符
    return [{"type": "text", "text": {"content": text[i : i + 1900]}} for i in range(0, max(len(text), 1), 1900)]


def to_blocks(md: str) -> list[dict]:
    """把简单 Markdown（# 标题、- 列表、数字列表、普通段落）转成 Notion 块。"""
    blocks: list[dict] = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            t = f"heading_{len(m.group(1))}"
            blocks.append({"object": "block", "type": t, t: {"rich_text": _rich(m.group(2))}})
        elif re.match(r"^\s*[-*•]\s+", line):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich(re.sub(r"^\s*[-*•]\s+", "", line))}})
        elif re.match(r"^\s*\d+[.、)]\s+", line):
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rich(re.sub(r"^\s*\d+[.、)]\s+", "", line))}})
        else:
            blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(line)}})
    return blocks


def _call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {os.environ['AICHAT_NOTION_TOKEN']}", "Notion-Version": VERSION, "Content-Type": "application/json", "User-Agent": "aichat-bridge/0.2"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Notion 返回 {e.code}: {e.read().decode(errors='replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 Notion：{e.reason}") from None


def create_page(title: str, markdown: str) -> str:
    """在配置的父页面下新建一页，返回页面链接。"""
    blocks = to_blocks(markdown)
    parent = os.environ["AICHAT_NOTION_PARENT"].strip()
    page = _call(
        "POST",
        "/pages",
        {"parent": {"page_id": parent}, "properties": {"title": {"title": _rich(title[:200])}}, "children": blocks[:100]},
    )
    for i in range(100, len(blocks), 100):  # 每次最多 100 块
        _call("PATCH", f"/blocks/{page['id']}/children", {"children": blocks[i : i + 100]})
    return page.get("url", "")
