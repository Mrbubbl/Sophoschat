// 全局状态与事件总线
const bus = new EventTarget();
export const on = (type, fn) => bus.addEventListener(type, (e) => fn(e.detail));
export const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

export const S = {
  me: null,
  users: [], // 可发起私聊的其他用户
  rooms: new Map(),
  cur: null, // 当前会话 id
  msgs: new Map(), // room_id -> { list, hasBefore, hasAfter }
  tasks: new Map(), // trigger_message_id -> 最新任务
  agent: null,
  online: false,
  view: 'list', // 窄屏：list | chat | panel
  panelOpen: window.innerWidth >= 1180,
  panelTab: 'memory',
  showArchived: false,
};

export function room(id) {
  return S.rooms.get(id);
}

export function userIndex() {
  const idx = new Map();
  if (S.me) idx.set(S.me.id, S.me);
  for (const u of S.users) idx.set(u.id, u);
  for (const r of S.rooms.values()) for (const m of r.members) idx.set(m.id, m);
  return idx;
}

export function userById(id) {
  return userIndex().get(id) || { id, display_name: '用户' + id, username: 'u' + id, role: 'user' };
}

export function otherHuman(r) {
  return r.members.find((m) => m.role !== 'agent' && m.id !== S.me.id);
}

export function roomTitle(r) {
  if (r.type === 'group') return r.title || '群聊';
  if (r.type === 'agent_dm') return 'Agent';
  const o = otherHuman(r);
  return o ? o.display_name : '私聊';
}

export function roomSubtitle(r) {
  if (r.type === 'agent_dm') return '与你的 Agent 私聊';
  if (r.type === 'group') return r.members.filter((m) => m.role !== 'agent').map((m) => m.display_name).join('、') + (r.agent_in_room ? ' · 含 Agent' : '');
  return r.agent_in_room ? '双人聊天 · 含 Agent' : '双人聊天';
}

export function agentUser(r) {
  return r.members.find((m) => m.role === 'agent');
}

export function agentStateText(st) {
  if (!st) return '';
  return { unconfigured: '未配置', offline: '离线', online: '在线' }[st.state] || st.state;
}
