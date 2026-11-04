"""链接预览：由服务器代为抓取页面的标题、摘要和封面图，所有人看到同一张卡片。

安全要点（用户可以让服务器去访问任意网址，所以必须防 SSRF）：
- 只允许 http/https、默认端口 80/443、不带账号密码；
- 域名先解析，所有解析结果都必须是公网地址；之后用这个 IP 去连接（防 DNS 重绑定），TLS 仍按域名校验；
- 重定向手动跟随，每一跳重新校验，最多 4 跳；
- 超时、下载大小、并发数、每用户频率都有上限；不携带任何 Cookie；
- 封面图只接受 JPG/PNG/WebP/GIF 且不超过 2 MB，由服务器缓存后再提供（页面的安全策略不允许直接加载外站图片）。
"""
from __future__ import annotations

import asyncio
import hashlib
import html as htmllib
import ipaddress
import json
import re
import socket
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx

from . import config, db
from .errors import ApiError

UA = "Mozilla/5.0 (compatible; SophosChatBot/1.0; link-preview)"
HTML_MAX = 600 * 1024
IMG_MAX = 2 * 1024 * 1024
OK_TTL = timedelta(days=7)
FAIL_TTL = timedelta(minutes=15)
RATE_PER_MIN = 20
MAX_REDIRECTS = 4
ALLOWED_PORTS = (None, 80, 443)
_sems: dict[int, asyncio.Semaphore] = {}


def _sem() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    if loop not in _sems:
        _sems.clear()
        _sems[loop] = asyncio.Semaphore(4)
    return _sems[loop]

_inflight: dict[str, asyncio.Future] = {}
_hits: dict[int, deque] = defaultdict(deque)
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_STATUS = re.compile(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d{5,25})")


class Unsafe(Exception):
    pass


class Failed(Exception):
    pass


# ------------------------------------------------------------------ 地址校验
def _ip_ok(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def clean_url(raw: str):
    raw = (raw or "").strip()
    if not raw or len(raw) > 2000:
        raise Unsafe("bad_url")
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise Unsafe("bad_url")
    try:
        port = u.port
    except ValueError:
        raise Unsafe("bad_url")
    if port not in ALLOWED_PORTS:
        raise Unsafe("bad_port")
    host = u.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")):
        raise Unsafe("bad_host")
    return u._replace(netloc=(host if port is None else f"{host}:{port}"), fragment="")


async def _resolve(host: str) -> str:
    try:
        ips = [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), 4)
        except Exception:
            raise Failed("dns")
        ips = [sa[0] for _, _, _, _, sa in infos]
    if not ips or not all(_ip_ok(i) for i in ips):
        raise Unsafe("private_address")
    ips.sort(key=lambda i: ":" in i)
    return ips[0]


async def _fetch(url: str, max_bytes: int, accept: str) -> tuple[str, str, bytes, bool]:
    """返回 (最终网址, Content-Type, 内容, 是否被截断)。"""
    cur = url
    timeout = httpx.Timeout(7.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}) as c:
        for _ in range(MAX_REDIRECTS + 1):
            u = clean_url(cur)
            ip = await _resolve(u.hostname)
            netloc = f"[{ip}]" if ":" in ip else ip
            if u.port:
                netloc += f":{u.port}"
            target = urlunparse((u.scheme, netloc, u.path or "/", u.params, u.query, ""))
            host_hdr = u.hostname.encode("idna").decode() + (f":{u.port}" if u.port else "")
            ext = {"sni_hostname": u.hostname.encode("idna").decode()} if u.scheme == "https" else {}
            try:
                async with c.stream("GET", target, headers={"Host": host_hdr, "Accept": accept}, extensions=ext) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        cur = urljoin(cur, r.headers["location"])
                        continue
                    if r.status_code >= 400:
                        raise Failed(f"http_{r.status_code}")
                    body = bytearray()
                    truncated = False
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            truncated = True
                            del body[max_bytes:]
                            break
                    return cur, (r.headers.get("content-type") or "").lower(), bytes(body), truncated
            except (httpx.HTTPError, OSError) as e:
                raise Failed(type(e).__name__)
    raise Failed("too_many_redirects")


# ------------------------------------------------------------------ 解析
class _Meta(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self.title:
            self._in_title = True
        elif tag == "meta":
            a = {k.lower(): (v or "") for k, v in attrs}
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "body":
            self.done = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _decode(body: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else None
    if not enc:
        m2 = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
        enc = m2.group(1).decode("ascii", "ignore") if m2 else "utf-8"
    try:
        return body.decode(enc, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _squash(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def parse_html(text: str, base_url: str) -> dict:
    p = _Meta()
    try:
        for i in range(0, len(text), 16384):
            p.feed(text[i : i + 16384])
            if p.done:
                break
    except Exception:
        pass
    m = p.meta
    img = m.get("og:image") or m.get("og:image:url") or m.get("twitter:image") or m.get("twitter:image:src") or ""
    if img:
        img = urljoin(base_url, img.strip())
        if urlparse(img).scheme not in ("http", "https"):
            img = ""
    host = urlparse(base_url).hostname or ""
    return {
        "title": _squash(m.get("og:title") or m.get("twitter:title") or p.title, 160),
        "description": _squash(m.get("og:description") or m.get("twitter:description") or m.get("description") or "", 300),
        "site": _squash(m.get("og:site_name") or host.removeprefix("www."), 60),
        "image": img,
    }


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.in_p = False

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.in_p = True
        elif tag == "br" and self.in_p:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self.in_p = False

    def handle_data(self, d):
        if self.in_p:
            self.parts.append(d)


def parse_oembed_x(data: dict) -> dict | None:
    t = _Text()
    t.feed(data.get("html") or "")
    text = re.sub(r"[ \t]+", " ", "".join(t.parts)).strip()
    author = _squash(data.get("author_name") or "", 60)
    if not text:
        return None
    return {"title": f"{author} 在 X 上发帖" if author else "X 帖子", "description": _squash(text, 300), "site": "X", "image": ""}


def _sniff_image(head: bytes) -> str | None:
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


# ------------------------------------------------------------------ 缓存与入口
def _key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _dir():
    d = config.get().data_dir / "linkpreviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ser(row) -> dict | None:
    if not row["ok"]:
        return None
    return {
        "url": row["url"],
        "title": row["title"] or "",
        "description": row["description"] or "",
        "site": row["site"] or "",
        "image_url": f"/api/link-preview/image/{row['key']}" if row["image_ok"] else None,
        "kind": "x" if row["site"] == "X" else "web",
    }


def _store(key: str, url: str, info: dict | None, image_ok: bool) -> None:
    now = _now()
    ok = bool(info and (info.get("title") or info.get("description")))
    ttl = OK_TTL if ok else FAIL_TTL
    db.run(
        "INSERT INTO link_previews(key,url,title,description,site,ok,image_ok,fetched_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET url=excluded.url,title=excluded.title,description=excluded.description,site=excluded.site,"
        "ok=excluded.ok,image_ok=excluded.image_ok,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at",
        (key, url, (info or {}).get("title"), (info or {}).get("description"), (info or {}).get("site"), 1 if ok else 0, 1 if image_ok else 0, now.isoformat(), (now + ttl).isoformat()),
    )
    if int(key[:4], 16) % 40 == 0:
        _gc()


def _gc() -> None:
    cutoff = (_now() - timedelta(days=30)).isoformat()
    for r in db.all("SELECT key FROM link_previews WHERE fetched_at<?", (cutoff,)):
        (_dir() / f"{r['key']}.bin").unlink(missing_ok=True)
    db.run("DELETE FROM link_previews WHERE fetched_at<?", (cutoff,))


async def _build(url: str, key: str) -> dict | None:
    u = clean_url(url)
    info = None
    host = (u.hostname or "").lower()
    if host in X_HOSTS:
        m = X_STATUS.match(u.path)
        if m:
            api = f"https://publish.twitter.com/oembed?omit_script=1&dnt=true&url={quote(f'https://twitter.com/{m.group(1)}/status/{m.group(2)}', safe='')}"
            try:
                _, _, body, _ = await _fetch(api, 100 * 1024, "application/json")
                info = parse_oembed_x(json.loads(body.decode("utf-8", "replace")))
            except (Failed, ValueError):
                info = None
    if info is None:
        final, ctype, body, _ = await _fetch(urlunparse(u), HTML_MAX, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
        if "html" not in ctype and "xml" not in ctype and ctype:
            raise Failed("not_html")
        info = parse_html(_decode(body, ctype), final)
    image_ok = False
    if info.get("image"):
        try:
            _, ictype, ibody, trunc = await _fetch(info["image"], IMG_MAX, "image/*")
            if not trunc and _sniff_image(ibody[:16]) and (not ictype or ictype.startswith(("image/", "application/octet-stream"))):
                (_dir() / f"{key}.bin").write_bytes(ibody)
                image_ok = True
        except (Failed, Unsafe):
            pass
    _store(key, url, info, image_ok)
    return info


def _rate_ok(user_id: int) -> bool:
    now = time.monotonic()
    q = _hits[user_id]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return False
    q.append(now)
    return True


async def get_preview(user_id: int, raw_url: str) -> dict | None:
    try:
        url = urlunparse(clean_url(raw_url))
    except Unsafe:
        raise ApiError(400, "bad_url", "这个链接不支持预览")
    key = _key(url)
    row = db.one("SELECT * FROM link_previews WHERE key=?", (key,))
    if row and row["expires_at"] > _now().isoformat():
        return _ser(row)
    if key in _inflight:
        await asyncio.shield(_inflight[key])
        row = db.one("SELECT * FROM link_previews WHERE key=?", (key,))
        return _ser(row) if row else None
    if not _rate_ok(user_id):
        raise ApiError(429, "too_many_previews", "预览请求太频繁，稍后再试")
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        async with _sem():
            try:
                info = await asyncio.wait_for(_build(url, key), 20)
            except (Failed, Unsafe, asyncio.TimeoutError):
                _store(key, url, None, False)
                info = None
        row = db.one("SELECT * FROM link_previews WHERE key=?", (key,))
        return _ser(row) if row else None
    finally:
        fut.set_result(None)
        _inflight.pop(key, None)


def image_path(key: str):
    if not re.fullmatch(r"[0-9a-f]{24}", key or ""):
        raise ApiError(404, "no_image")
    row = db.one("SELECT image_ok FROM link_previews WHERE key=?", (key,))
    p = _dir() / f"{key}.bin"
    if not row or not row["image_ok"] or not p.exists():
        raise ApiError(404, "no_image")
    mime = _sniff_image(p.read_bytes()[:16])
    if not mime:
        raise ApiError(404, "no_image")
    return p, mime
