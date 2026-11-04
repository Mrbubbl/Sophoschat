"""v10：链接预览（含 SSRF 防护）。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from aichat import linkpreview as lp

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_address[1]
        path = self.path.split("?")[0]
        if path == "/og":
            self._send(200, f"""<html><head><title>fallback</title>
<meta property="og:title" content="项目 &amp; 方案"><meta property="og:description" content="  很长的\n  描述  ">
<meta property="og:site_name" content="示例站"><meta property="og:image" content="/img.png"></head><body>x</body></html>""".encode())
        elif path == "/img.png":
            self._send(200, PNG, "image/png")
        elif path == "/gbk":
            self._send(200, "<html><head><title>中文标题</title></head></html>".encode("gbk"), "text/html; charset=gbk")
        elif path == "/plain":
            self._send(200, b"<html><head><title>Just a title</title></head><body></body></html>")
        elif path == "/empty":
            self._send(200, b"<html><head></head><body>nothing</body></html>")
        elif path == "/redir":
            self._send(302, extra={"Location": "/og"})
        elif path == "/redir-bad":
            self._send(302, extra={"Location": f"http://127.0.0.2:{port}/og"})
        elif path == "/loop":
            self._send(302, extra={"Location": "/loop"})
        elif path == "/big":
            self._send(200, b"<html><head><title>Big</title></head><body>" + b"x" * (900 * 1024))
        elif path == "/notimg":
            self._send(200, b'<html><head><title>T</title><meta property="og:image" content="/fake.png"></head></html>')
        elif path == "/fake.png":
            self._send(200, b"<html>not an image</html>", "image/png")
        elif path == "/json":
            self._send(200, b"{}", "application/json")
        else:
            self._send(404, b"nope")


@pytest.fixture
def site(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(lp, "_ip_ok", lambda ip: ip == "127.0.0.1")
    monkeypatch.setattr(lp, "ALLOWED_PORTS", (None, 80, 443, port))
    lp._hits.clear()
    lp._inflight.clear()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


def get(env, url):
    r = env.a.get("/api/link-preview", params={"url": url})
    assert r.status_code == 200, r.text
    return r.json()["preview"]


def test_open_graph_with_image_proxy(env, site):
    p = get(env, site + "/og")
    assert p["title"] == "项目 & 方案" and p["description"] == "很长的 描述" and p["site"] == "示例站"
    assert p["image_url"].startswith("/api/link-preview/image/")
    img = env.b.get(p["image_url"])  # 任何已登录成员都能看到同一张卡片
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content == PNG
    assert env.anon.get(p["image_url"]).status_code == 401
    # 第二次走缓存：关掉服务器也能拿到
    assert get(env, site + "/og")["title"] == "项目 & 方案"


def test_redirect_charset_and_fallbacks(env, site):
    assert get(env, site + "/redir")["title"] == "项目 & 方案"
    assert get(env, site + "/gbk")["title"] == "中文标题"
    p = get(env, site + "/plain")
    assert p["title"] == "Just a title" and p["image_url"] is None and p["site"] == "127.0.0.1"
    assert get(env, site + "/empty") is None
    assert get(env, site + "/missing") is None
    assert get(env, site + "/json") is None
    assert get(env, site + "/loop") is None


def test_big_page_and_fake_image(env, site):
    assert get(env, site + "/big")["title"] == "Big"  # 只读前 600 KB 也够用
    p = get(env, site + "/notimg")
    assert p["title"] == "T" and p["image_url"] is None  # 声明是图片但内容不是，不保存


def test_ssrf_redirect_to_private_is_blocked(env, site):
    assert get(env, site + "/redir-bad") is None


def test_ssrf_never_connects(env, monkeypatch):
    calls = []
    real = httpx.AsyncClient.stream

    def spy(self, *a, **k):
        calls.append(a)
        return real(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "stream", spy)
    lp._hits.clear()
    for u in ["http://127.0.0.1/", "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://192.168.1.1/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/"]:
        assert get(env, u) is None, u
    assert calls == []
    for bad in ["file:///etc/passwd", "ftp://example.com/", "http://user:pw@example.com/", "http://example.com:22/", "http://localhost/", "http://foo.internal/", "javascript:alert(1)", "http://", ""]:
        r = env.a.get("/api/link-preview", params={"url": bad})
        assert r.status_code == 400, bad
    assert env.anon.get("/api/link-preview", params={"url": "http://example.com/"}).status_code == 401


def test_x_post_uses_oembed(env, monkeypatch):
    lp._hits.clear()
    seen = []

    async def fake_fetch(url, max_bytes, accept):
        seen.append(url)
        assert url.startswith("https://publish.twitter.com/oembed?")
        data = {"author_name": "Some One", "html": '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">Hello <a href="https://t.co/x">world</a><br>second line</p>&mdash; Some One (@someone) <a href="https://twitter.com/someone/status/1234567890123">Nov 1</a></blockquote>'}
        return url, "application/json", json.dumps(data).encode(), False

    monkeypatch.setattr(lp, "_fetch", fake_fetch)
    p = get(env, "https://x.com/someone/status/1234567890123?s=20")
    assert p["kind"] == "x" and p["site"] == "X" and p["title"] == "Some One 在 X 上发帖"
    assert p["description"] == "Hello world\nsecond line".replace("\n", " ") or "second line" in p["description"]
    assert len(seen) == 1


def test_rate_limit_and_negative_cache(env, site, monkeypatch):
    monkeypatch.setattr(lp, "RATE_PER_MIN", 3)
    lp._hits.clear()
    for i in range(3):
        get(env, site + f"/plain?i={i}")
    r = env.a.get("/api/link-preview", params={"url": site + "/plain?i=99"})
    assert r.status_code == 429
    # 已缓存的不占额度
    assert get(env, site + "/plain?i=0")["title"] == "Just a title"
    # 别的用户不受影响
    assert env.b.get("/api/link-preview", params={"url": site + "/plain?i=77"}).status_code == 200


def test_pref_toggle_and_cleanup(env):
    assert env.a.get("/api/me").json()["prefs"]["link_preview"] is True
    assert env.a.put("/api/me/prefs", json={"link_preview": False}).json()["prefs"]["link_preview"] is False
    assert env.a.put("/api/me/prefs", json={"link_preview": "yes"}).status_code == 400
    assert env.a.get("/api/link-preview/image/../../etc/passwd").status_code in (404, 422)
    assert env.a.get("/api/link-preview/image/" + "0" * 24).status_code == 404


def test_preview_storage_shim_is_injected_once_at_the_top():
    from aichat.artifact_api import STORAGE_SHIM, with_shim

    a = with_shim("<!doctype html><html><head><title>t</title></head><body><script>localStorage.getItem('x')</script></body></html>")
    assert a.index(STORAGE_SHIM) < a.index("<title>") and a.count(STORAGE_SHIM) == 1
    assert with_shim("<html><body>x</body></html>").index(STORAGE_SHIM) < with_shim("<html><body>x</body></html>").index("<body>")
    assert with_shim("<p>fragment</p>").startswith(STORAGE_SHIM)
    assert "localStorage" in STORAGE_SHIM and "fetch" not in STORAGE_SHIM  # 只补存储，不放开网络
