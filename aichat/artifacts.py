"""Room-authorized immutable files. Generated content is data, never server code."""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

from . import auth, chat, config, db, runtime, tasks
from .errors import ApiError


class HtmlInspector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = set()
        self.external = []
        self.refresh = False

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        a = dict(attrs)
        for key in ("src", "srcset", "action", "poster"):
            if a.get(key) and not a[key].startswith(("data:", "#")):
                self.external.append(f"{tag}.{key}")
        if tag == "link" and a.get("href"):
            self.external.append("link.href")
        if tag in ("iframe", "object", "embed", "base"):
            self.external.append(tag)
        if tag == "meta" and (a.get("http-equiv") or "").lower() == "refresh":
            self.refresh = True


def inspect_file(filename: str, content: str, limit: int) -> tuple[str, str, bytes, dict]:
    if not isinstance(content, str) or not content.strip() or "\x00" in content:
        raise ApiError(422, "invalid_document", "文件必须是非空 UTF-8 文本")
    if not isinstance(filename, str) or filename != Path(filename).name or "\\" in filename or len(filename) > 160:
        raise ApiError(422, "invalid_filename", "请使用不含目录的文件名")
    ext = Path(filename).suffix.lower()
    if ext not in (".md", ".html"):
        raise ApiError(422, "unsupported_document", "当前交付支持 .md 和自包含 .html")
    raw = content.encode("utf-8")
    if len(raw) > limit:
        raise ApiError(413, "file_too_large", "交付物超过任务文件大小限制")
    checks = {"utf8": True, "nonempty": True, "size": len(raw)}
    if ext == ".html":
        p = HtmlInspector()
        try:
            p.feed(content)
        except Exception:
            raise ApiError(422, "invalid_html") from None
        if not {"html", "head", "body"}.issubset(p.tags):
            raise ApiError(422, "incomplete_html", "HTML 必须包含 html、head 和 body")
        if p.external or p.refresh or re.search(r"(?:@import|url\(\s*['\"]?(?:https?:|//))", content, re.I):
            raise ApiError(422, "external_html_resource", "首版网页需自包含，不允许远程资源、嵌套页面或自动跳转")
        checks["self_contained"] = True
        checks["browser"] = {"status": "not_verified"}
        return "html", "text/html; charset=utf-8", raw, checks
    return "markdown", "text/markdown; charset=utf-8", raw, checks


def version_row(version_id: int):
    v = db.one("SELECT v.*,a.room_id,a.title,a.kind,a.initiator_id,a.current_version_id,a.deleted_at FROM artifact_versions v JOIN artifacts a ON a.id=v.artifact_id WHERE v.id=?", (version_id,))
    if not v or v["deleted_at"]:
        raise ApiError(404, "artifact_not_found")
    return v


def serialize(v) -> dict:
    return {k: v[k] for k in ("id", "artifact_id", "number", "parent_version_id", "task_id", "filename", "mime", "size", "sha256", "created_at", "message_id")} | {
        "room_id": v["room_id"], "title": v["title"], "kind": v["kind"],
        "checks": db.jd(v["checks_json"], {}), "current_version_id": v["current_version_id"],
        "download_url": f"/api/artifact-versions/{v['id']}/download",
        "content_url": f"/api/artifact-versions/{v['id']}/content",
        "preview_url": f"/api/artifact-versions/{v['id']}/preview" if v["kind"] == "html" else None,
    }


def authorized(version_id: int, user_id: int):
    v = version_row(version_id)
    member = chat.require_member(v["room_id"], user_id)
    m = db.one("SELECT seq,deleted_at FROM messages WHERE id=?", (v["message_id"],))
    if not m or m["deleted_at"] or m["seq"] <= member["history_from_seq"]:
        raise ApiError(404, "artifact_not_found")
    return v


def file_path(v) -> Path:
    p = config.get().data_dir / v["path"]
    if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != v["sha256"]:
        raise ApiError(409, "artifact_missing_or_corrupt", "文件缺失或校验失败，请从备份恢复")
    return p


def publish(t, agent_id: int, data: dict) -> dict:
    if t["profile"] != "delivery":
        raise ApiError(403, "delivery_only", "只有获授权的交付任务可以发布文件")
    s = runtime.settings()
    kind, mime, raw, checks = inspect_file(data.get("filename", ""), data.get("content", ""), s["max_file_bytes"])
    slot = data.get("slot", "main")
    if slot != "main":
        raise ApiError(422, "invalid_slot", "首版每个交付任务生成一个文件；新文件使用独立任务")
    if kind == "html" and isinstance(data.get("browser_check"), dict):
        checks["browser"] = data["browser_check"]
        if checks["browser"].get("status") == "failed":
            raise ApiError(422, "html_interaction_check_failed")
    digest = hashlib.sha256(raw).hexdigest()
    base = version_row(t["base_version_id"]) if t["base_version_id"] else None
    if base and (base["room_id"] != t["room_id"] or base["kind"] != kind):
        raise ApiError(409, "artifact_base_mismatch")
    directory = config.get().data_dir / "artifacts" / str(t["room_id"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (uuid.uuid4().hex + (".html" if kind == "html" else ".md"))
    try:
        with db.tx():
            existing = db.one("SELECT id FROM artifact_versions WHERE task_id=? AND slot=?", (t["id"], slot))
            if existing:
                v = version_row(existing["id"])
                if v["sha256"] != digest:
                    raise ApiError(409, "artifact_already_published", "本任务已有不可变交付，请读取现有结果；后续修改建立新任务")
                return serialize(v)
            if base:
                current = db.one("SELECT current_version_id FROM artifacts WHERE id=?", (base["artifact_id"],))
                if current["current_version_id"] != base["id"]:
                    raise ApiError(409, "artifact_version_conflict", "此文件已有新版，请读取当前版本后重新发起改稿")
                aid, number = base["artifact_id"], base["number"] + 1
            else:
                aid = db.insert("INSERT INTO artifacts(room_id,kind,title,initiator_id,created_at) VALUES(?,?,?,?,?)", (t["room_id"], kind, (data.get("title") or data["filename"])[:160], t["initiator_id"], db.now()))
                number = 1
            with target.open("xb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            vid = db.insert("INSERT INTO artifact_versions(artifact_id,number,parent_version_id,task_id,slot,filename,mime,path,size,sha256,checks_json,source_message_id,is_test,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (aid, number, base["id"] if base else None, t["id"], slot, data["filename"], mime, str(target.relative_to(config.get().data_dir)), len(raw), digest, db.jl(checks), t["trigger_message_id"], int(bool(db.jd(t["params"], {}).get("selfcheck"))), db.now()))
            # File, version, and message are a single database publication. A
            # network retry cannot create a second card or mutate the old one.
            msg, _ = chat.send_message(t["room_id"], agent_id, f"已交付 {data['filename']} · v{number}" + (f"（基于 v{number-1}）" if base else "") + ("\n" + str(data.get("change_summary", ""))[:500] if data.get("change_summary") else ""), client_msg_id=f"artifact-{t['id']}-{slot}", reply_to=t["trigger_message_id"], artifact_version_id=vid, is_test=bool(db.jd(t["params"], {}).get("selfcheck")))
            db.run("UPDATE artifact_versions SET message_id=? WHERE id=?", (msg["id"], vid))
            db.run("UPDATE artifacts SET current_version_id=? WHERE id=?", (vid, aid))
            runtime.receipt(t, "artifact.published", str(vid))
            runtime.receipt(t, "artifact.verified", str(vid))
            tasks._publish_task(t["id"])
        return serialize(version_row(vid))
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def task_versions(t) -> list[dict]:
    return [serialize(version_row(r["id"])) for r in db.all("SELECT id FROM artifact_versions WHERE task_id=? ORDER BY id", (t["id"],))]


def start_delivery(t, instruction: str, base_version_id: int | None = None) -> dict:
    if t["kind"] != "chat" or t["profile"] != "chat" or not t["initiator_id"]:
        raise ApiError(403, "chat_request_required")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 20000:
        raise ApiError(422, "instruction_required")
    with db.tx():
        existing = db.one("SELECT * FROM agent_tasks WHERE parent_task_id=? AND profile='delivery' ORDER BY id LIMIT 1", (t["id"],))
        if existing:
            return tasks.ser_task(existing)
        if base_version_id:
            base = authorized(base_version_id, t["initiator_id"])
            if base["room_id"] != t["room_id"]:
                raise ApiError(404, "artifact_not_found")
            if base["initiator_id"] != t["initiator_id"] and auth.get_user(t["initiator_id"])["role"] != "owner":
                raise ApiError(403, "task_controller_only")
        source = db.one("SELECT * FROM messages WHERE id=?", (t["trigger_message_id"],))
        tid = db.insert("INSERT INTO agent_tasks(room_id,kind,profile,trigger_message_id,trigger_message_version,initiator_id,status,created_at,expires_at,params,parent_task_id,base_version_id) VALUES(?,'chat','delivery',?,?,?,'queued',?,?,?,?,?)",
                        (t["room_id"], source["id"], source["version"], t["initiator_id"], db.now(), db.after(86400), db.jl({"instruction": instruction}), t["id"], base_version_id))
        tasks._publish_task(tid)
        tasks._wake()
        return tasks.ser_task(db.one("SELECT * FROM agent_tasks WHERE id=?", (tid,)))
