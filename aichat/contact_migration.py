"""One-time reversible archiving of legacy empty DMs; also supports read-only preview."""
import json
from datetime import datetime, timezone

KEY = 'contacts_migration_v10'


def preview(c):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    done = c.execute('SELECT value FROM kv WHERE key=?', (KEY,)).fetchone() if 'kv' in tables else None
    if done:
        return {'applied': True, 'rooms': 0, 'memberships': 0, 'previous': json.loads(done[0])}
    if not {'rooms', 'room_members', 'messages'} <= tables:
        return {'applied': False, 'rooms': 0, 'memberships': 0}
    extra = ''.join(f' AND NOT EXISTS(SELECT 1 FROM {t} x WHERE x.room_id=r.id)' for t in ('artifacts', 'agent_tasks') if t in tables)
    rows = c.execute("SELECT r.id FROM rooms r WHERE r.type='dm' AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.room_id=r.id)" + extra).fetchall()
    ids = [r[0] for r in rows]
    n = sum(c.execute("SELECT COUNT(*) FROM room_members m JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.archived=0 AND u.role!='agent'", (rid,)).fetchone()[0] for rid in ids)
    return {'applied': False, 'rooms': len(ids), 'memberships': n, 'room_ids': ids}


def apply(c):
    plan = preview(c)
    if plan['applied']:
        return plan
    for rid in plan.pop('room_ids', []):
        c.execute("UPDATE room_members SET archived=1 WHERE room_id=? AND user_id IN (SELECT id FROM users WHERE role!='agent')", (rid,))
    result = {**plan, 'applied': True, 'at': datetime.now(timezone.utc).isoformat()}
    c.execute('INSERT INTO kv(key,value) VALUES(?,?)', (KEY, json.dumps(result)))
    return result
