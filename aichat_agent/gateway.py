from __future__ import annotations

import httpx


class GatewayError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class Gateway:
    def __init__(self, url: str, token: str):
        self.client = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=35, trust_env=False, headers={"Authorization": "Bearer " + token})

    async def call(self, method: str, path: str, body=None, *, params=None, proof=None):
        headers = {"X-Task-Token": proof} if proof else None
        r = await self.client.request(method, path, json=body, params=params, headers=headers)
        if r.is_error:
            try:
                code = r.json().get("error", {}).get("code", "request_failed")
            except ValueError:
                code = "request_failed"
            raise GatewayError(r.status_code, code)
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    async def close(self):
        await self.client.aclose()
