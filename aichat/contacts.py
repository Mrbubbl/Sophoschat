"""Per-user address books. Saving a contact never creates a conversation."""
from . import auth, db, hub
from .errors import ApiError


def public(user):
    return {k: v for k, v in auth.public_user(user).items()
            if k in ('id', 'username', 'display_name', 'role', 'disabled', 'avatar_url')}


def list_for(owner_id):
    return [public(r) for r in db.all(
        'SELECT u.* FROM contacts c JOIN users u ON u.id=c.contact_user_id WHERE c.owner_user_id=? ORDER BY u.display_name COLLATE NOCASE,u.username COLLATE NOCASE,u.id', (owner_id,))]


def require(owner_id, target_id):
    u = auth.get_user(target_id)
    if not u or u['disabled'] or u['role'] == 'agent':
        raise ApiError(404, 'user_not_found', '未找到可添加的账号')
    if not db.one('SELECT 1 FROM contacts WHERE owner_user_id=? AND contact_user_id=?', (owner_id, target_id)):
        raise ApiError(403, 'contact_required', '请先将对方添加为联系人')
    return u


def add(owner_id, target_id):
    if owner_id == target_id:
        raise ApiError(400, 'invalid_target', '不能添加自己')
    u = auth.get_user(target_id)
    if not u or u['disabled'] or u['role'] == 'agent':
        raise ApiError(404, 'user_not_found', '未找到可添加的账号')
    with db.tx():
        db.run('INSERT OR IGNORE INTO contacts(owner_user_id,contact_user_id,created_at) VALUES(?,?,?)', (owner_id, target_id, db.now()))
        db.on_commit(lambda: hub.publish([owner_id], {'type': 'contacts.updated'}))
    return public(u)


def remove(owner_id, target_id):
    with db.tx():
        db.run('DELETE FROM contacts WHERE owner_user_id=? AND contact_user_id=?', (owner_id, target_id))
        db.on_commit(lambda: hub.publish([owner_id], {'type': 'contacts.updated'}))


def lookup(owner_id, username):
    username = username.strip()
    if not auth.USERNAME_RE.fullmatch(username):
        raise ApiError(400, 'invalid_username', '请输入完整用户名（2–32 位字母、数字、下划线或短横线）')
    # Persist the window across restarts; no global username directory is returned.
    key = f'contacts_lookup:{owner_id}'
    with db.tx():
        import time
        recent = [t for t in db.jd(db.get_kv(key), []) if t > time.time() - 60]
        if len(recent) >= 20:
            raise ApiError(429, 'too_many_attempts', '查找过于频繁，请一分钟后再试')
        db.set_kv(key, db.jl([*recent, time.time()]))
    u = db.one("SELECT * FROM users WHERE username=? AND disabled=0 AND role!='agent'", (username,))
    if not u:
        raise ApiError(404, 'user_not_found', '未找到可添加的账号')
    if u['id'] == owner_id:
        raise ApiError(400, 'invalid_target', '不能添加自己')
    return public(u)


def profile_audience(target_id):
    """People who already know this account, plus existing account managers."""
    ids = {target_id}
    ids.update(r['owner_user_id'] for r in db.all('SELECT owner_user_id FROM contacts WHERE contact_user_id=?', (target_id,)))
    ids.update(r['user_id'] for r in db.all('SELECT DISTINCT a.user_id FROM room_members a JOIN room_members b ON a.room_id=b.room_id WHERE b.user_id=? AND b.left_at IS NULL AND a.left_at IS NULL', (target_id,)))
    ids.update(r['id'] for r in db.all("SELECT id FROM users WHERE disabled=0 AND (role='owner' OR can_add_users=1)"))
    return ids
