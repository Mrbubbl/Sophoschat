"""SQLite 访问层：单连接 + 显式事务 + 提交后回调（用于 WebSocket 推送）。

整个服务在一个事件循环线程里运行，数据库函数都是同步的、中间没有 await，
因此单连接不会交错；多进程（CLI、备份）靠 WAL 与 busy_timeout 协调。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

_conn: sqlite3.Connection | None = None
_depth = 0
_after: list[Callable[[], None]] = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ago(seconds: float) -> str:
    return after(-seconds)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  password_hash TEXT,
  role TEXT NOT NULL CHECK(role IN ('owner','user','agent')),
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  user_agent TEXT
);
CREATE TABLE IF NOT EXISTS credentials(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT,
  created_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL CHECK(type IN ('dm','group','agent_dm')),
  title TEXT,
  created_by INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL,
  retention_days INTEGER,
  consolidation_enabled INTEGER NOT NULL DEFAULT 1,
  max_seq INTEGER NOT NULL DEFAULT 0,
  dm_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS room_members(
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  joined_at TEXT NOT NULL,
  last_read_seq INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  muted INTEGER NOT NULL DEFAULT 0,
  history_from_seq INTEGER NOT NULL DEFAULT 0,
  left_at TEXT,
  PRIMARY KEY(room_id, user_id)
);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  uploader_id INTEGER NOT NULL REFERENCES users(id),
  message_id INTEGER,
  filename TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL REFERENCES rooms(id),
  seq INTEGER NOT NULL,
  sender_id INTEGER NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK(kind IN ('human','agent')),
  body TEXT,
  reply_to INTEGER REFERENCES messages(id),
  attachment_id INTEGER REFERENCES attachments(id),
  client_msg_id TEXT,
  mentions TEXT NOT NULL DEFAULT '[]',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  edited_at TEXT,
  deleted_at TEXT,
  UNIQUE(room_id, seq),
  UNIQUE(room_id, sender_id, client_msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_room_time ON messages(room_id, created_at);
CREATE TABLE IF NOT EXISTS message_versions(
  message_id INTEGER NOT NULL REFERENCES messages(id),
  version INTEGER NOT NULL,
  body TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(message_id, version)
);
CREATE TABLE IF NOT EXISTS stars(
  user_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(user_id, message_id)
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  message_id INTEGER,
  message_version INTEGER,
  actor_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_room ON events(room_id, id);
CREATE TABLE IF NOT EXISTS cursors(
  consumer TEXT NOT NULL,
  room_id INTEGER NOT NULL,
  last_event_id INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY(consumer, room_id)
);
CREATE TABLE IF NOT EXISTS agent_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('chat','consolidate')),
  trigger_message_id INTEGER,
  trigger_message_version INTEGER,
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','done','failed','expired','cancelled')),
  lease_owner TEXT,
  lease_credential_id INTEGER,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  finished_at TEXT,
  result_message_id INTEGER,
  error TEXT,
  params TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON agent_tasks(status, kind, id);
CREATE TABLE IF NOT EXISTS summaries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  period_start TEXT,
  period_end TEXT,
  content TEXT NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]',
  stale INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  overview TEXT NOT NULL DEFAULT '',
  overview_stale INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','merged')),
  merged_into INTEGER,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_room ON topics(room_id, status);
CREATE TABLE IF NOT EXISTS memory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id INTEGER NOT NULL,
  topic_id INTEGER REFERENCES topics(id),
  type TEXT NOT NULL,
  content TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  author_user_id INTEGER,
  author_kind TEXT NOT NULL CHECK(author_kind IN ('human','agent')),
  nature TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'current' CHECK(state IN ('current','superseded','disputed','unverified','retired')),
  adoption TEXT CHECK(adoption IN ('none','adopted','rejected','partial')),
  occurred_at TEXT,
  valid_from TEXT,
  valid_to TEXT,
  sources TEXT NOT NULL DEFAULT '[]',
  external_refs TEXT NOT NULL DEFAULT '[]',
  supersedes INTEGER,
  manual INTEGER NOT NULL DEFAULT 0,
  manual_origin INTEGER NOT NULL DEFAULT 0,
  source_stale INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_room ON memory_items(room_id, topic_id);
CREATE TABLE IF NOT EXISTS memory_item_versions(
  item_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  state TEXT NOT NULL,
  adoption TEXT,
  sources TEXT NOT NULL DEFAULT '[]',
  reason TEXT,
  actor TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(item_id, version)
);
CREATE TABLE IF NOT EXISTS tombstones(
  room_id INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(room_id, fingerprint)
);
CREATE TABLE IF NOT EXISTS commit_log(
  idempotency_key TEXT PRIMARY KEY,
  room_id INTEGER NOT NULL,
  task_id INTEGER,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  room_id INTEGER,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions(
  message_id INTEGER NOT NULL REFERENCES messages(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  emoji TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS push_subscriptions(
  endpoint TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  user_agent TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

SCHEMA_VERSION = 4

# 旧库升级：只做加列，不删不改已有数据
_ADD_COLUMNS = [
    ("sessions", "last_seen_at", "TEXT"),
    ("sessions", "ip", "TEXT"),
    ("attachments", "duration_ms", "INTEGER"),
    ("attachments", "transcript", "TEXT"),
    ("users", "avatar", "TEXT"),
    ("users", "can_add_users", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(c: sqlite3.Connection) -> None:
    for table, col, ddl in _ADD_COLUMNS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def connect(path) -> sqlite3.Connection:
    global _conn, _depth
    close()
    c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA synchronous=NORMAL")
    _conn = c
    _depth = 0
    _after.clear()
    c.executescript(SCHEMA)
    ver = c.execute("PRAGMA user_version").fetchone()[0]
    if ver < SCHEMA_VERSION:
        _migrate(c)
    return c


def close() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        finally:
            _conn = None


def conn() -> sqlite3.Connection:
    assert _conn is not None, "database not initialised"
    return _conn


@contextmanager
def tx():
    """事务；可嵌套（内层并入外层）。提交成功后才执行 on_commit 注册的回调。"""
    global _depth
    c = conn()
    if _depth > 0:
        _depth += 1
        try:
            yield c
        finally:
            _depth -= 1
        return
    c.execute("BEGIN IMMEDIATE")
    _depth = 1
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        _after.clear()
        try:
            c.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        _depth = 0
    callbacks = list(_after)
    _after.clear()
    for fn in callbacks:
        try:
            fn()
        except Exception:  # 推送失败不能影响已提交的数据
            pass


def on_commit(fn: Callable[[], None]) -> None:
    if _depth > 0:
        _after.append(fn)
    else:
        fn()


def one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(args)).fetchone()


def all_(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, tuple(args)).fetchall()


all = all_  # noqa: A001  - 统一用 db.all(...)，与 db.one(...) 对称


def run(sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
    return conn().execute(sql, tuple(args))


def insert(sql: str, args: Iterable[Any] = ()) -> int:
    cur = conn().execute(sql, tuple(args))
    return int(cur.lastrowid)


def jl(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def jd(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def get_kv(key: str, default: str | None = None) -> str | None:
    r = one("SELECT value FROM kv WHERE key=?", (key,))
    return r["value"] if r else default


def set_kv(key: str, value: str | None) -> None:
    run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def audit(actor: str, action: str, room_id: int | None = None, **detail: Any) -> None:
    run(
        "INSERT INTO audit_log(actor, action, room_id, detail, created_at) VALUES(?,?,?,?,?)",
        (actor, action, room_id, jl(detail), now()),
    )
