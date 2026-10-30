"""极简 HTTP 客户端（只用标准库，Bridge 可以单独部署在任何有 Python 的机器上）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class BridgeApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", extra: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    # 这些错误意味着“任务已不属于我了”：应当静默放弃，而不是上报失败或重试写入
    ABANDON = {"lease_not_held", "credential_revoked", "agent_not_in_room", "invalid_task_token", "source_deleted"}

    @property
    def abandon(self) -> bool:
        return self.code in self.ABANDON


MAX_DOWNLOAD = 26 * 1024 * 1024


class Http:
    def __init__(self, base_url: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, token: str | None = None, body: dict | None = None, params: dict | None = None, timeout: int | None = None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get("content-type", "")
                return json.loads(raw) if "json" in ctype else raw.decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            try:
                err = json.loads(raw).get("error", {})
            except Exception:
                err = {}
            extra = {k: v for k, v in err.items() if k not in ("code", "message")}
            raise BridgeApiError(e.code, err.get("code", "http_error"), err.get("message", raw[:200]), extra) from None
        except urllib.error.URLError as e:
            raise BridgeApiError(0, "network_error", str(e.reason)) from None

    def get_bytes(self, path: str, token: str, timeout: int = 90) -> tuple[bytes, str]:
        """下载二进制（图片/语音）；超过 26MB 直接放弃。"""
        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_DOWNLOAD + 1)
                if len(data) > MAX_DOWNLOAD:
                    raise BridgeApiError(413, "too_large", "文件过大")
                return data, r.headers.get("content-type", "application/octet-stream")
        except urllib.error.HTTPError as e:
            raise BridgeApiError(e.code, "http_error", e.read().decode(errors="replace")[:200]) from None
        except urllib.error.URLError as e:
            raise BridgeApiError(0, "network_error", str(e.reason)) from None
