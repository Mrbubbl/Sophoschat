"""长期记忆：稳定主题、记忆条目、时间段回顾；原子提交、版本、来源校验、人工纠正保护、失效传播。

设计取舍：服务器不做任何模型推理。语义判断（归并到哪个主题、是否修订、采纳状态）由外部 Agent 给出，
服务器负责：作用域、来源有效性、版本冲突、幂等、人工纠正优先、游标原子推进。
"""
from __future__ import annotations

import hashlib
import json
import re

from . import db, hub
from .errors import ApiError

ITEM_TYPES = {"opinion", "suggestion", "fact", "conclusion", "question", "plan", "preference", "other"}
NATURES = {"stated", "agent_suggestion", "reported_fact", "speculation", "conclusion", "verified_fact"}
OPEN_STATES = ("current", "disputed", "unverified")
ADOPTIONS = {"none", "adopted", "rejected", "partial"}
ITEM_STATES = {"current", "superseded", "disputed", "unverified", "retired"}


# ---------------------------------------------------------------- 工具函数
def fingerprint(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()


def terms(q: str) -> list[str]:
    q = (q or "").lower()
    words = re.findall(r"[a-z0-9_]{2,}", q)
    grams: list[str] = []
    for run in re.findall(r"[一-鿿]+", q):
        grams += [run] if len(run) == 1 else [run[i : i + 2] for i in range(len(run) - 1)]
    return list(dict.fromkeys(words + grams))[:40]


def _norm_sources(raw) -> list[dict]:
    out, seen = [], set()
    for s in raw or []:
        try:
            mid, ver = int(s["message_id"]), int(s["version"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "invalid_source", "来源必须包含 message_id 与 version")
        if (mid, ver) not in seen:
            seen.add((mid, ver))
            out.append({"message_id": mid, "version": ver})
    return out


def validate_sources(room_id: int, sources: list[dict]) -> list:
    """来源必须属于本 Room、未删除、版本为当前版本；否则整批拒绝，不静默写入。"""
    rows = []
    for s in sources:
        m = db.one("SELECT * FROM messages WHERE id=?", (s["message_id"],))
        if not m or m["room_id"] != room_id:
            raise ApiError(422, "invalid_source", "来源不在当前会话中", message_id=s["message_id"])
        if m["deleted_at"]:
            raise ApiError(409, "stale_source", "来源消息已被删除", message_id=s["message_id"], reason="deleted")
        if m["version"] != s["version"]:
            raise ApiError(409, "stale_source", "来源消息已被编辑，请重新读取", message_id=s["message_id"], reason="edited", current_version=m["version"])
        rows.append(m)
    return rows


def verify_sources(room_id: int, sources: list[dict]) -> list[dict]:
    out = []
    for s in _norm_sources(sources):
        m = db.one("SELECT id, room_id, version, deleted_at FROM messages WHERE id=?", (s["message_id"],))
        if not m or m["room_id"] != room_id:
            out.append({**s, "valid": False, "reason": "not_in_room"})
        elif m["deleted_at"]:
            out.append({**s, "valid": False, "reason": "deleted"})
        elif m["version"] != s["version"]:
            out.append({**s, "valid": False, "reason": "edited", "current_version": m["version"]})
        else:
            out.append({**s, "valid": True, "reason": "ok"})
    return out


def _human_ids(room_id: int) -> list[int]:
    from . import chat

    return chat.human_member_ids(room_id)


def _notify(room_id: int) -> None:
    db.on_commit(lambda: hub.publish(_human_ids(room_id), {"type": "memory.updated", "room_id": room_id}))


# ---------------------------------------------------------------- 序列化
def ser_item(r) -> dict:
    return {
        "id": r["id"],
        "room_id": r["room_id"],
        "topic_id": r["topic_id"],
        "type": r["type"],
        "content": r["content"],
        "author_user_id": r["author_user_id"],
        "author_kind": r["author_kind"],
        "nature": r["nature"],
        "state": r["state"],
        "adoption": r["adoption"],
        "occurred_at": r["occurred_at"],
        "valid_from": r["valid_from"],
        "valid_to": r["valid_to"],
        "sources": db.jd(r["sources"], []),
        "external_refs": db.jd(r["external_refs"], []),
        "supersedes": r["supersedes"],
        "manual": bool(r["manual"]),
        "manual_origin": bool(r["manual_origin"]),
        "source_stale": bool(r["source_stale"]),
        "version": r["version"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def ser_topic(r, with_counts: bool = True) -> dict:
    d = {
        "id": r["id"],
        "room_id": r["room_id"],
        "title": r["title"],
        "aliases": db.jd(r["aliases"], []),
        "overview": r["overview"],
        "overview_stale": bool(r["overview_stale"]),
        "status": r["status"],
        "merged_into": r["merged_into"],
        "version": r["version"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }
    if with_counts:
        d["item_count"] = db.one("SELECT COUNT(*) AS c FROM memory_items WHERE topic_id=? AND state IN ('current','disputed','unverified')", (r["id"],))["c"]
        d["stale_count"] = db.one("SELECT COUNT(*) AS c FROM memory_items WHERE topic_id=? AND source_stale=1", (r["id"],))["c"]
    return d


# ---------------------------------------------------------------- 读取
def list_topics(room_id: int, status: str | None = "active", q: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM topics WHERE room_id=?", [room_id]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY updated_at DESC"
    rows = db.all(sql, args)
    if q:
        ts = terms(q)
        scored = []
        for r in rows:
            hay = (r["title"] + " " + r["aliases"] + " " + r["overview"]).lower()
            s = sum(1 for t in ts if t in hay)
            if s:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        rows = [r for _, r in scored]
    return [ser_topic(r) for r in rows]


def pending_items(room_id: int) -> list[dict]:
    rows = db.all("SELECT * FROM memory_items WHERE room_id=? AND topic_id IS NULL AND state IN ('current','disputed','unverified') ORDER BY id DESC", (room_id,))
    return [ser_item(r) for r in rows]


def get_topic(topic_id: int, room_id: int, include_history: bool = False) -> dict:
    t = db.one("SELECT * FROM topics WHERE id=? AND room_id=?", (topic_id, room_id))
    if not t:
        raise ApiError(404, "topic_not_found", "主题不存在")
    items = db.all("SELECT * FROM memory_items WHERE topic_id=? ORDER BY CASE state WHEN 'current' THEN 0 WHEN 'disputed' THEN 1 WHEN 'unverified' THEN 2 ELSE 3 END, id", (topic_id,))
    out = ser_topic(t)
    out["items"] = [ser_item(i) for i in items]
    if include_history:
        for it in out["items"]:
            it["history"] = item_history(it["id"], room_id)
    return out


def item_history(item_id: int, room_id: int) -> list[dict]:
    it = db.one("SELECT id FROM memory_items WHERE id=? AND room_id=?", (item_id, room_id))
    if not it:
        raise ApiError(404, "item_not_found", "记忆条目不存在")
    rows = db.all("SELECT * FROM memory_item_versions WHERE item_id=? ORDER BY version", (item_id,))
    return [{"version": r["version"], "content": r["content"], "state": r["state"], "adoption": r["adoption"], "sources": db.jd(r["sources"], []), "reason": r["reason"], "actor": r["actor"], "created_at": r["created_at"]} for r in rows]


def get_item(item_id: int, room_id: int) -> dict:
    r = db.one("SELECT * FROM memory_items WHERE id=? AND room_id=?", (item_id, room_id))
    if not r:
        raise ApiError(404, "item_not_found", "记忆条目不存在")
    return ser_item(r)


def search_memory(room_id: int, q: str, limit: int = 10) -> dict:
    """只在当前 Room 内检索；先限定作用域，再排序。"""
    ts = terms(q)
    if not ts:
        return {"items": [], "topics": []}
    topics = {t["id"]: t for t in db.all("SELECT * FROM topics WHERE room_id=? AND status='active'", (room_id,))}
    scored_items = []
    for r in db.all("SELECT * FROM memory_items WHERE room_id=? AND state IN ('current','disputed','unverified','superseded')", (room_id,)):
        t = topics.get(r["topic_id"])
        hay = (r["content"] + " " + (t["title"] + " " + t["aliases"] if t else "")).lower()
        s = sum(1 for x in ts if x in hay)
        if s:
            scored_items.append((s, r))
    scored_items.sort(key=lambda x: (-x[0], -x[1]["id"]))
    scored_topics = []
    for t in topics.values():
        hay = (t["title"] + " " + t["aliases"] + " " + t["overview"]).lower()
        s = sum(1 for x in ts if x in hay)
        if s:
            scored_topics.append((s, t))
    scored_topics.sort(key=lambda x: -x[0])
    return {"items": [ser_item(r) for _, r in scored_items[:limit]], "topics": [ser_topic(t) for _, t in scored_topics[:5]]}


def list_summaries(room_id: int, limit: int = 50) -> list[dict]:
    rows = db.all("SELECT * FROM summaries WHERE room_id=? ORDER BY COALESCE(period_end, created_at) DESC LIMIT ?", (room_id, limit))
    return [
        {"id": r["id"], "period_start": r["period_start"], "period_end": r["period_end"], "content": r["content"], "sources": db.jd(r["sources"], []), "stale": bool(r["stale"]), "created_at": r["created_at"]}
        for r in rows
    ]


# ---------------------------------------------------------------- 批次读取（整理任务用）
def build_batch(task) -> dict:
    from . import chat

    room_id = task["room_id"]
    params = db.jd(task["params"], {})
    mode = params.get("mode", "pending")
    agent = chat.agent_member_id(room_id)
    hist = db.one("SELECT history_from_seq FROM room_members WHERE room_id=? AND user_id=?", (room_id, agent))["history_from_seq"]
    events: list[dict] = []
    if mode == "pending":
        rows = db.all("SELECT * FROM events WHERE room_id=? AND id>? AND id<=? ORDER BY id", (room_id, params["event_from"], params["event_to"]))
        events = [{"id": e["id"], "type": e["type"], "message_id": e["message_id"], "message_version": e["message_version"], "created_at": e["created_at"]} for e in rows]
        ids = list(dict.fromkeys(e["message_id"] for e in rows if e["message_id"]))
        msgs = []
        if ids:
            q = ",".join("?" * len(ids))
            msgs = db.all(f"SELECT * FROM messages WHERE id IN ({q}) AND seq>? ORDER BY seq", ids + [hist])
    else:
        msgs = db.all("SELECT * FROM messages WHERE room_id=? AND seq>=? AND seq<=? AND seq>? ORDER BY seq", (room_id, params["from_seq"], params["to_seq"], hist))
    ctx = []
    if msgs:
        lo = msgs[0]["seq"]
        ctx = list(reversed(db.all("SELECT * FROM messages WHERE room_id=? AND seq<? AND seq>? AND deleted_at IS NULL ORDER BY seq DESC LIMIT 15", (room_id, lo, hist))))
    stale = []
    for m in msgs:
        for it in db.all("SELECT DISTINCT i.* FROM memory_items i, json_each(i.sources) j WHERE i.room_id=? AND i.source_stale=1 AND json_extract(j.value,'$.message_id')=?", (room_id, m["id"])):
            if it["id"] not in {x["id"] for x in stale}:
                stale.append(ser_item(it))
    return {
        "task_id": task["id"],
        "mode": mode,
        "event_from": params.get("event_from"),
        "event_to": params.get("event_to"),
        "room": {"id": room_id},
        "members": [dict(id=m["id"], username=m["username"], display_name=m["display_name"], role=m["role"]) for m in chat.member_rows(room_id)],
        "events": events,
        "messages": chat.ser_messages(msgs),
        "context_before": chat.ser_messages(ctx),
        "stale_items": stale,
    }


# ---------------------------------------------------------------- 提交
def _touch_topic(topic_id: int | None) -> None:
    if topic_id:
        db.run("UPDATE topics SET updated_at=? WHERE id=?", (db.now(), topic_id))


def _item_ver(item_id: int, version: int, content: str, state: str, adoption, sources, reason: str, actor: str) -> None:
    db.run(
        "INSERT INTO memory_item_versions(item_id, version, content, state, adoption, sources, reason, actor, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (item_id, version, content, state, adoption, db.jl(sources), reason, actor, db.now()),
    )


def _resolve_topic(room_id: int, ref, refs: dict, allow_none: bool = True):
    if ref is None:
        if allow_none:
            return None
        raise ApiError(400, "topic_required", "需要指定主题")
    if isinstance(ref, (int, str)) and not isinstance(ref, bool):
        ref = {"id": int(ref)} if str(ref).isdigit() else {"ref": str(ref)}
    if "ref" in ref:
        if ref["ref"] not in refs:
            raise ApiError(400, "unknown_ref", f"未知的主题引用 {ref['ref']}")
        return refs[ref["ref"]]
    t = db.one("SELECT * FROM topics WHERE id=? AND room_id=?", (int(ref["id"]), room_id))
    if not t:
        raise ApiError(404, "topic_not_found", "主题不存在或不在本会话")
    hops = 0
    while t["status"] == "merged" and t["merged_into"] and hops < 5:  # 旧 ID 重定向
        t = db.one("SELECT * FROM topics WHERE id=?", (t["merged_into"],))
        hops += 1
    if t["status"] != "active":
        raise ApiError(409, "topic_inactive", "主题已归档")
    return t["id"]


def _check_author(room_id: int, user_id, kind: str):
    if user_id is None:
        return None
    u = db.one("SELECT * FROM users WHERE id=?", (int(user_id),))
    if not u:
        raise ApiError(422, "invalid_author", "作者不存在")
    if (u["role"] == "agent") != (kind == "agent"):
        raise ApiError(422, "author_kind_mismatch", "author_kind 与作者身份不一致")
    if not db.one("SELECT 1 FROM room_members WHERE room_id=? AND user_id=?", (room_id, u["id"])):
        raise ApiError(422, "invalid_author", "作者不是本会话成员")
    return u["id"]


def _check_adoption(adoption, author_kind: str, msgs: list) -> None:
    if adoption is None:
        return
    if adoption not in ADOPTIONS:
        raise ApiError(400, "invalid_adoption", f"adoption 必须是 {sorted(ADOPTIONS)}")
    if author_kind != "agent":
        raise ApiError(422, "adoption_not_applicable", "采纳状态只适用于 Agent 来源的建议")
    if adoption in ("adopted", "partial", "rejected") and not any(m["kind"] == "human" for m in msgs):
        raise ApiError(422, "adoption_needs_human_source", "宣称已采纳/已拒绝必须附带用户本人的确认消息作为来源")


def _op_topic_create(room_id, op, refs, res):
    title = str(op.get("title", "")).strip()[:80]
    if not title:
        raise ApiError(400, "title_required", "主题名称不能为空")
    aliases = [str(a).strip()[:40] for a in (op.get("aliases") or []) if str(a).strip()][:10]
    low = {title.lower(), *(a.lower() for a in aliases)}
    for t in db.all("SELECT * FROM topics WHERE room_id=? AND status='active'", (room_id,)):
        have = {t["title"].lower(), *(a.lower() for a in db.jd(t["aliases"], []))}
        if low & have:
            raise ApiError(409, "topic_exists", "已存在同名/同别名主题，请更新它而不是新建", existing_topic_id=t["id"])
    tid = db.insert(
        "INSERT INTO topics(room_id, title, aliases, overview, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (room_id, title, db.jl(aliases), str(op.get("overview", ""))[:2000], db.now(), db.now()),
    )
    if op.get("ref"):
        refs[str(op["ref"])] = tid
    res["topics"][str(op.get("ref") or tid)] = tid


def _op_topic_update(room_id, op, refs, res):
    t = db.one("SELECT * FROM topics WHERE id=? AND room_id=?", (int(op.get("topic_id", 0)), room_id))
    if not t:
        raise ApiError(404, "topic_not_found", "主题不存在")
    if int(op.get("expected_version", -1)) != t["version"]:
        raise ApiError(409, "version_conflict", "主题已被修改，请重新读取", current_version=t["version"], topic_id=t["id"])
    title, aliases, overview, status = t["title"], db.jd(t["aliases"], []), t["overview"], t["status"]
    stale = t["overview_stale"]
    if "title" in op:
        title = str(op["title"]).strip()[:80]
        if not title:
            raise ApiError(400, "title_required", "主题名称不能为空")
    if "aliases" in op:
        aliases = [str(a).strip()[:40] for a in (op["aliases"] or []) if str(a).strip()][:10]
    if "overview" in op:
        overview = str(op["overview"])[:2000]
        stale = 0
    if "status" in op:
        if op["status"] not in ("active", "archived"):
            raise ApiError(400, "invalid_status")
        status = op["status"]
    db.run(
        "UPDATE topics SET title=?, aliases=?, overview=?, overview_stale=?, status=?, version=version+1, updated_at=? WHERE id=?",
        (title, db.jl(aliases), overview, stale, status, db.now(), t["id"]),
    )
    res["updated_topics"].append(t["id"])


def _merge_topics(room_id: int, from_id: int, into_id: int, actor: str) -> None:
    a = db.one("SELECT * FROM topics WHERE id=? AND room_id=?", (from_id, room_id))
    b = db.one("SELECT * FROM topics WHERE id=? AND room_id=?", (into_id, room_id))
    if not a or not b or from_id == into_id:
        raise ApiError(404, "topic_not_found", "主题不存在")
    if a["status"] != "active" or b["status"] != "active":
        raise ApiError(409, "topic_inactive", "只能合并活动主题")
    for it in db.all("SELECT * FROM memory_items WHERE topic_id=?", (from_id,)):
        db.run("UPDATE memory_items SET topic_id=?, version=version+1, updated_at=? WHERE id=?", (into_id, db.now(), it["id"]))
        _item_ver(it["id"], it["version"] + 1, it["content"], it["state"], it["adoption"], db.jd(it["sources"], []), f"topic merged {from_id}->{into_id}", actor)
    aliases = list(dict.fromkeys(db.jd(b["aliases"], []) + [a["title"]] + db.jd(a["aliases"], [])))[:10]
    db.run("UPDATE topics SET aliases=?, version=version+1, updated_at=?, overview_stale=1 WHERE id=?", (db.jl(aliases), db.now(), into_id))
    db.run("UPDATE topics SET status='merged', merged_into=?, version=version+1, updated_at=? WHERE id=?", (into_id, db.now(), from_id))


def _op_item_create(room_id, op, refs, res, actor):
    type_ = op.get("type", "other")
    if type_ not in ITEM_TYPES:
        raise ApiError(400, "invalid_type", f"type 必须是 {sorted(ITEM_TYPES)}")
    content = str(op.get("content", "")).strip()
    if not content or len(content) > 2000:
        raise ApiError(400, "invalid_content", "内容不能为空且不超过 2000 字")
    kind = op.get("author_kind")
    if kind not in ("human", "agent"):
        raise ApiError(400, "invalid_author_kind", "author_kind 必须是 human 或 agent")
    nature = op.get("nature")
    if nature not in NATURES:
        raise ApiError(400, "invalid_nature", f"nature 必须是 {sorted(NATURES)}")
    state = op.get("state", "current")
    if state not in OPEN_STATES:
        raise ApiError(400, "invalid_state", f"新建条目的 state 必须是 {list(OPEN_STATES)}")
    sources = _norm_sources(op.get("sources"))
    if not sources:
        raise ApiError(400, "sources_required", "自动提取的记忆必须带来源消息 ID 与版本")
    msgs = validate_sources(room_id, sources)
    author = _check_author(room_id, op.get("author_user_id"), kind)
    if nature in ("stated", "agent_suggestion"):
        if author is None or not any(m["sender_id"] == author for m in msgs):
            raise ApiError(422, "author_not_in_sources", "观点/建议的作者必须是其中一条来源消息的发送者")
    adoption = op.get("adoption")
    if nature == "agent_suggestion" and adoption is None:
        adoption = "none"
    _check_adoption(adoption, kind, msgs)
    topic_id = _resolve_topic(room_id, op.get("topic"), refs)
    fp = fingerprint(content)
    if db.one("SELECT 1 FROM tombstones WHERE room_id=? AND fingerprint=?", (room_id, fp)):
        res["skipped"].append({"reason": "tombstoned"})
        return
    dup = db.one(
        "SELECT * FROM memory_items WHERE room_id=? AND fingerprint=? AND state IN ('current','disputed','unverified') AND ((topic_id IS NULL AND ? IS NULL) OR topic_id=?)",
        (room_id, fp, topic_id, topic_id),
    )
    if dup:
        merged = _norm_sources(db.jd(dup["sources"], []) + sources)
        if len(merged) != len(db.jd(dup["sources"], [])):
            db.run("UPDATE memory_items SET sources=?, version=version+1, updated_at=? WHERE id=?", (db.jl(merged), db.now(), dup["id"]))
            _item_ver(dup["id"], dup["version"] + 1, dup["content"], dup["state"], dup["adoption"], merged, "duplicate: sources linked", actor)
        res["skipped"].append({"reason": "deduplicated", "item_id": dup["id"]})
        return
    superseded = [int(x) for x in (op.get("supersedes") or [])]
    for sid in superseded:
        old = db.one("SELECT * FROM memory_items WHERE id=? AND room_id=?", (sid, room_id))
        if not old:
            raise ApiError(404, "item_not_found", "被替代的条目不存在", item_id=sid)
        if old["manual"]:
            raise ApiError(409, "manual_correction_locked", "该条目经过人工纠正，不能被自动替代", item_id=sid)
    iid = db.insert(
        "INSERT INTO memory_items(room_id, topic_id, type, content, fingerprint, author_user_id, author_kind, nature, state, adoption, occurred_at, valid_from, valid_to, sources, external_refs, supersedes, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (room_id, topic_id, type_, content, fp, author, kind, nature, state, adoption, op.get("occurred_at"), op.get("valid_from"), op.get("valid_to"), db.jl(sources), db.jl(op.get("external_refs") or []), superseded[0] if superseded else None, db.now(), db.now()),
    )
    _item_ver(iid, 1, content, state, adoption, sources, str(op.get("reason", "extracted"))[:200], actor)
    for sid in superseded:
        old = db.one("SELECT * FROM memory_items WHERE id=?", (sid,))
        db.run("UPDATE memory_items SET state='superseded', version=version+1, updated_at=? WHERE id=?", (db.now(), sid))
        _item_ver(sid, old["version"] + 1, old["content"], "superseded", old["adoption"], db.jd(old["sources"], []), f"superseded by item {iid}: {str(op.get('reason',''))[:150]}", actor)
    _touch_topic(topic_id)
    res["items"].append(iid)


def _op_item_update(room_id, op, refs, res, actor):
    it = db.one("SELECT * FROM memory_items WHERE id=? AND room_id=?", (int(op.get("item_id", 0)), room_id))
    if not it:
        raise ApiError(404, "item_not_found", "记忆条目不存在")
    if int(op.get("expected_version", -1)) != it["version"]:
        raise ApiError(409, "version_conflict", "条目已被修改，请重新读取", current_version=it["version"], item_id=it["id"])
    changes_semantic = any(k in op for k in ("content", "state", "adoption", "topic"))
    if it["manual"] and changes_semantic:
        raise ApiError(409, "manual_correction_locked", "该条目经过人工纠正，Agent 不能改写；如有新证据请新建条目并标记分歧", item_id=it["id"])
    content, state, adoption, topic_id = it["content"], it["state"], it["adoption"], it["topic_id"]
    sources = db.jd(it["sources"], [])
    new_sources = _norm_sources(op.get("add_sources"))
    new_msgs = validate_sources(room_id, new_sources) if new_sources else []
    if "content" in op:
        content = str(op["content"]).strip()
        if not content or len(content) > 2000:
            raise ApiError(400, "invalid_content", "内容不能为空且不超过 2000 字")
        if db.one("SELECT 1 FROM tombstones WHERE room_id=? AND fingerprint=?", (room_id, fingerprint(content))):
            raise ApiError(409, "tombstoned", "该内容已被用户删除，不能再次写入")
    if "state" in op:
        if op["state"] not in ITEM_STATES:
            raise ApiError(400, "invalid_state")
        state = op["state"]
    if "adoption" in op:
        adoption = op["adoption"]
        _check_adoption(adoption, it["author_kind"], new_msgs)
    if "topic" in op:
        topic_id = _resolve_topic(room_id, op["topic"], refs)
    if changes_semantic and not str(op.get("reason", "")).strip():
        raise ApiError(400, "reason_required", "修改条目必须说明原因")
    if new_sources:
        sources = _norm_sources(sources + new_sources)
    stale = it["source_stale"]
    if op.get("confirm_sources"):
        refreshed = []
        for s in sources:
            m = db.one("SELECT version, deleted_at FROM messages WHERE id=?", (s["message_id"],))
            if m and not m["deleted_at"]:
                refreshed.append({"message_id": s["message_id"], "version": m["version"]})
        sources, stale = refreshed, 0
    if (content, state, adoption, topic_id, db.jl(sources)) == (it["content"], it["state"], it["adoption"], it["topic_id"], it["sources"]) and stale == it["source_stale"]:
        return
    db.run(
        "UPDATE memory_items SET content=?, fingerprint=?, state=?, adoption=?, topic_id=?, sources=?, source_stale=?, version=version+1, updated_at=? WHERE id=?",
        (content, fingerprint(content), state, adoption, topic_id, db.jl(sources), stale, db.now(), it["id"]),
    )
    _item_ver(it["id"], it["version"] + 1, content, state, adoption, sources, str(op.get("reason", "update"))[:200], actor)
    _touch_topic(topic_id)
    _touch_topic(it["topic_id"])
    res["updated_items"].append(it["id"])


def _apply_op(room_id, op, refs, res, actor):
    if not isinstance(op, dict) or "op" not in op:
        raise ApiError(400, "invalid_op", "每个操作必须是带 op 字段的对象")
    name = op["op"]
    if name == "topic.create":
        _op_topic_create(room_id, op, refs, res)
    elif name == "topic.update":
        _op_topic_update(room_id, op, refs, res)
    elif name == "topic.merge":
        _merge_topics(room_id, int(op.get("from_topic_id", 0)), int(op.get("into_topic_id", 0)), actor)
        res["updated_topics"] += [int(op["from_topic_id"]), int(op["into_topic_id"])]
    elif name == "item.create":
        _op_item_create(room_id, op, refs, res, actor)
    elif name == "item.update":
        _op_item_update(room_id, op, refs, res, actor)
    elif name == "item.retire":
        _op_item_update(room_id, {**op, "state": "retired"}, refs, res, actor)
    else:
        raise ApiError(400, "unknown_op", f"不支持的操作 {name}")


def _apply_summary(room_id: int, s: dict, res: dict) -> None:
    content = str(s.get("content", "")).strip()
    if not content or len(content) > 4000:
        raise ApiError(400, "invalid_content", "回顾内容不能为空且不超过 4000 字")
    sources = _norm_sources(s.get("sources"))
    if not sources:
        raise ApiError(400, "sources_required", "回顾必须带来源消息")
    validate_sources(room_id, sources)
    sid = db.insert(
        "INSERT INTO summaries(room_id, period_start, period_end, content, sources, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        (room_id, s.get("period_start"), s.get("period_end"), content, db.jl(sources), db.now(), db.now()),
    )
    res["summaries"].append(sid)


def commit_batch(task, agent_id: int, payload: dict) -> dict:
    """整理结果的原子提交：全部成功并推进游标，或全部不生效。"""
    room_id = task["room_id"]
    params = db.jd(task["params"], {})
    mode = params.get("mode", "pending")
    key = str(payload.get("idempotency_key") or f"task-{task['id']}")[:120]
    ops, summaries = payload.get("ops") or [], payload.get("summaries") or []
    if not isinstance(ops, list) or not isinstance(summaries, list) or len(ops) > 200 or len(summaries) > 50:
        raise ApiError(400, "invalid_payload", "ops/summaries 必须是列表（ops ≤ 200，summaries ≤ 50）")
    actor = f"agent:{agent_id}"
    with db.tx():
        prev = db.one("SELECT result FROM commit_log WHERE idempotency_key=?", (key,))
        if prev:
            r = json.loads(prev["result"])
            r["replayed"] = True
            return r
        if mode == "pending" and payload.get("event_to") != params["event_to"]:
            raise ApiError(400, "event_to_mismatch", "event_to 必须等于任务领取到的 event_to", expected=params["event_to"])
        refs: dict = {}
        res = {"topics": {}, "items": [], "updated_items": [], "updated_topics": [], "skipped": [], "summaries": []}
        for i, op in enumerate(ops):
            try:
                _apply_op(room_id, op, refs, res, actor)
            except ApiError as e:
                e.extra.setdefault("op_index", i)
                raise
        for s in summaries:
            _apply_summary(room_id, s, res)
        if mode == "pending":
            db.run(
                "INSERT INTO cursors(consumer, room_id, last_event_id, updated_at) VALUES('consolidate',?,?,?) "
                "ON CONFLICT(consumer, room_id) DO UPDATE SET last_event_id=MAX(last_event_id, excluded.last_event_id), updated_at=excluded.updated_at",
                (room_id, params["event_to"], db.now()),
            )
        db.set_kv("last_consolidation_at", db.now())
        result = {"ok": True, "cursor": params.get("event_to"), **res, "replayed": False}
        db.run("INSERT INTO commit_log(idempotency_key, room_id, task_id, result, created_at) VALUES(?,?,?,?,?)", (key, room_id, task["id"], json.dumps(result), db.now()))
        db.audit(actor, "memory.commit", room_id, task_id=task["id"], items=len(res["items"]), updated=len(res["updated_items"]), topics=len(res["topics"]))
        _notify(room_id)
    return result


# ---------------------------------------------------------------- 人工维护（优先于自动提取）
def _member(room_id: int, user_id: int):
    from . import chat

    return chat.require_member(room_id, user_id)


def human_update_item(user_id: int, item_id: int, changes: dict) -> dict:
    with db.tx():
        it = db.one("SELECT * FROM memory_items WHERE id=?", (item_id,))
        if not it:
            raise ApiError(404, "item_not_found", "记忆条目不存在")
        _member(it["room_id"], user_id)
        content, state, adoption, topic_id = it["content"], it["state"], it["adoption"], it["topic_id"]
        if "content" in changes:
            content = str(changes["content"]).strip()
            if not content or len(content) > 2000:
                raise ApiError(400, "invalid_content", "内容不能为空且不超过 2000 字")
        if "state" in changes:
            if changes["state"] not in ITEM_STATES:
                raise ApiError(400, "invalid_state")
            state = changes["state"]
        if "adoption" in changes:
            if changes["adoption"] is not None and changes["adoption"] not in ADOPTIONS:
                raise ApiError(400, "invalid_adoption")
            adoption = changes["adoption"]
        if "topic_id" in changes:
            tid = changes["topic_id"]
            if tid is not None and not db.one("SELECT 1 FROM topics WHERE id=? AND room_id=? AND status='active'", (tid, it["room_id"])):
                raise ApiError(404, "topic_not_found", "目标主题不存在")
            topic_id = tid
        db.run(
            "UPDATE memory_items SET content=?, fingerprint=?, state=?, adoption=?, topic_id=?, manual=1, version=version+1, updated_at=? WHERE id=?",
            (content, fingerprint(content), state, adoption, topic_id, db.now(), item_id),
        )
        _item_ver(item_id, it["version"] + 1, content, state, adoption, db.jd(it["sources"], []), str(changes.get("reason", "manual correction"))[:200], f"user:{user_id}")
        _touch_topic(topic_id)
        _touch_topic(it["topic_id"])
        db.audit(f"user:{user_id}", "memory.item.update", it["room_id"], item_id=item_id)
        _notify(it["room_id"])
        return ser_item(db.one("SELECT * FROM memory_items WHERE id=?", (item_id,)))


def human_delete_item(user_id: int, item_id: int) -> None:
    with db.tx():
        it = db.one("SELECT * FROM memory_items WHERE id=?", (item_id,))
        if not it:
            raise ApiError(404, "item_not_found", "记忆条目不存在")
        _member(it["room_id"], user_id)
        # 只留下内容指纹作为“不要再生成”的标记，不保存正文
        db.run("INSERT OR IGNORE INTO tombstones(room_id, fingerprint, created_at) VALUES(?,?,?)", (it["room_id"], it["fingerprint"], db.now()))
        db.run("DELETE FROM memory_item_versions WHERE item_id=?", (item_id,))
        db.run("DELETE FROM memory_items WHERE id=?", (item_id,))
        _touch_topic(it["topic_id"])
        db.audit(f"user:{user_id}", "memory.item.delete", it["room_id"], item_id=item_id)
        _notify(it["room_id"])


def human_create_item(user_id: int, room_id: int, topic_id: int | None, content: str, type_: str = "other") -> dict:
    content = (content or "").strip()
    if not content or len(content) > 2000:
        raise ApiError(400, "invalid_content", "内容不能为空且不超过 2000 字")
    if type_ not in ITEM_TYPES:
        type_ = "other"
    with db.tx():
        _member(room_id, user_id)
        if topic_id is not None and not db.one("SELECT 1 FROM topics WHERE id=? AND room_id=? AND status='active'", (topic_id, room_id)):
            raise ApiError(404, "topic_not_found", "主题不存在")
        iid = db.insert(
            "INSERT INTO memory_items(room_id, topic_id, type, content, fingerprint, author_user_id, author_kind, nature, state, manual, manual_origin, sources, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'human','stated','current',1,1,'[]',?,?)",
            (room_id, topic_id, type_, content, fingerprint(content), user_id, db.now(), db.now()),
        )
        _item_ver(iid, 1, content, "current", None, [], "manual", f"user:{user_id}")
        _touch_topic(topic_id)
        _notify(room_id)
        return ser_item(db.one("SELECT * FROM memory_items WHERE id=?", (iid,)))


def human_update_topic(user_id: int, topic_id: int, changes: dict) -> dict:
    with db.tx():
        t = db.one("SELECT * FROM topics WHERE id=?", (topic_id,))
        if not t:
            raise ApiError(404, "topic_not_found")
        _member(t["room_id"], user_id)
        title, aliases, overview, status = t["title"], db.jd(t["aliases"], []), t["overview"], t["status"]
        stale = t["overview_stale"]
        if "title" in changes:
            title = str(changes["title"]).strip()[:80]
            if not title:
                raise ApiError(400, "title_required", "主题名称不能为空")
        if "aliases" in changes:
            aliases = [str(a).strip()[:40] for a in changes["aliases"] if str(a).strip()][:10]
        if "overview" in changes:
            overview, stale = str(changes["overview"])[:2000], 0
        if "status" in changes:
            if changes["status"] not in ("active", "archived"):
                raise ApiError(400, "invalid_status")
            status = changes["status"]
        db.run("UPDATE topics SET title=?, aliases=?, overview=?, overview_stale=?, status=?, version=version+1, updated_at=? WHERE id=?", (title, db.jl(aliases), overview, stale, status, db.now(), topic_id))
        db.audit(f"user:{user_id}", "memory.topic.update", t["room_id"], topic_id=topic_id)
        _notify(t["room_id"])
        return ser_topic(db.one("SELECT * FROM topics WHERE id=?", (topic_id,)))


def human_merge_topics(user_id: int, from_id: int, into_id: int) -> dict:
    with db.tx():
        a = db.one("SELECT room_id FROM topics WHERE id=?", (from_id,))
        if not a:
            raise ApiError(404, "topic_not_found")
        _member(a["room_id"], user_id)
        _merge_topics(a["room_id"], from_id, into_id, f"user:{user_id}")
        db.audit(f"user:{user_id}", "memory.topic.merge", a["room_id"], from_id=from_id, into_id=into_id)
        _notify(a["room_id"])
        return ser_topic(db.one("SELECT * FROM topics WHERE id=?", (into_id,)))


# ---------------------------------------------------------------- 失效传播
def _citing_items(room_id: int, message_id: int):
    return db.all(
        "SELECT DISTINCT i.* FROM memory_items i, json_each(i.sources) j WHERE i.room_id=? AND json_extract(j.value,'$.message_id')=?",
        (room_id, message_id),
    )


def _citing_summaries(room_id: int, message_id: int):
    return db.all(
        "SELECT DISTINCT s.* FROM summaries s, json_each(s.sources) j WHERE s.room_id=? AND json_extract(j.value,'$.message_id')=?",
        (room_id, message_id),
    )


def on_message_edited(room_id: int, message_id: int, new_version: int) -> None:
    """原文被编辑：依赖它的条目、概览、回顾标记为待重算，旧版本不再被当作当前依据。"""
    touched = False
    for it in _citing_items(room_id, message_id):
        db.run("UPDATE memory_items SET source_stale=1, updated_at=? WHERE id=?", (db.now(), it["id"]))
        if it["topic_id"]:
            db.run("UPDATE topics SET overview_stale=1 WHERE id=?", (it["topic_id"],))
        touched = True
    for s in _citing_summaries(room_id, message_id):
        db.run("UPDATE summaries SET stale=1, updated_at=? WHERE id=?", (db.now(), s["id"]))
        touched = True
    if touched:
        _notify(room_id)


def on_message_deleted(room_id: int, message_id: int) -> None:
    """原文被删除：仅由该来源支撑的派生内容被清理；还有其他来源的保留并标记待核对。"""
    touched = False
    for it in _citing_items(room_id, message_id):
        remaining = [s for s in db.jd(it["sources"], []) if s["message_id"] != message_id]
        if it["topic_id"]:
            db.run("UPDATE topics SET overview_stale=1 WHERE id=?", (it["topic_id"],))
        if not remaining and not it["manual_origin"] and not it["manual"]:
            db.run("INSERT OR IGNORE INTO tombstones(room_id, fingerprint, created_at) VALUES(?,?,?)", (room_id, it["fingerprint"], db.now()))
            db.run("DELETE FROM memory_item_versions WHERE item_id=?", (it["id"],))
            db.run("DELETE FROM memory_items WHERE id=?", (it["id"],))
        elif not remaining:
            db.run("UPDATE memory_items SET sources='[]', updated_at=? WHERE id=?", (db.now(), it["id"]))
        else:
            db.run("UPDATE memory_items SET sources=?, source_stale=1, updated_at=? WHERE id=?", (db.jl(remaining), db.now(), it["id"]))
        touched = True
    for s in _citing_summaries(room_id, message_id):
        remaining = [x for x in db.jd(s["sources"], []) if x["message_id"] != message_id]
        if not remaining:
            db.run("DELETE FROM summaries WHERE id=?", (s["id"],))
        else:
            db.run("UPDATE summaries SET sources=?, stale=1, updated_at=? WHERE id=?", (db.jl(remaining), db.now(), s["id"]))
        touched = True
    if touched:
        _notify(room_id)
