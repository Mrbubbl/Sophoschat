"""Additive schema for server SDK execution and immutable delivered files."""

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS sdk_sessions(
 session_key TEXT PRIMARY KEY, room_id INTEGER NOT NULL, profile TEXT NOT NULL,
 scope_hash TEXT NOT NULL, config_revision INTEGER NOT NULL,
 sdk_session_id TEXT, total_cost REAL, usage_json TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_attempts(
 task_id INTEGER NOT NULL, attempt INTEGER NOT NULL, generation TEXT NOT NULL UNIQUE,
 room_id INTEGER NOT NULL, initiator_id INTEGER, profile TEXT NOT NULL,
 model TEXT, sdk_version TEXT, config_json TEXT NOT NULL DEFAULT '{}',
 reserved_usd REAL NOT NULL DEFAULT 0, cost_usd REAL, usage_json TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT,
 PRIMARY KEY(task_id,attempt)
);
CREATE TABLE IF NOT EXISTS artifacts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('markdown','html')),
 title TEXT NOT NULL, initiator_id INTEGER NOT NULL,
 current_version_id INTEGER, created_at TEXT NOT NULL, deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS artifact_versions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
 number INTEGER NOT NULL, parent_version_id INTEGER, task_id INTEGER NOT NULL,
 slot TEXT NOT NULL DEFAULT 'main', filename TEXT NOT NULL, mime TEXT NOT NULL,
 path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
 checks_json TEXT NOT NULL DEFAULT '{}', source_message_id INTEGER,
 message_id INTEGER, is_test INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(artifact_id,number), UNIQUE(task_id,slot)
);
CREATE TABLE IF NOT EXISTS task_tool_receipts(
 task_id INTEGER NOT NULL, attempt INTEGER NOT NULL, action TEXT NOT NULL,
 object_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,attempt,action,object_id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_started ON task_attempts(started_at,initiator_id,room_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_room ON artifacts(room_id,id);
"""
