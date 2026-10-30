// 左侧：会话列表、全局搜索、新建聊天
import { get, post, qs } from './api.js';
import { S, emit, on, room, roomTitle, otherHuman, agentUser, userById } from './store.js';
import { $, h, icon, iconBtn, avatar, fmtRel, debounce, modal, toast, fmtDay, fmtTime } from './util.js';

const els = {};

export function initRooms(container) {
  els.search = h('input', { type: 'search', id: 'gsearch', placeholder: '搜索全部聊天记录', 'aria-label': '搜索全部聊天记录', autocomplete: 'off' });
  els.tabs = h('div', { class: 'tabs', role: 'tablist' });
  els.list = h('div', { class: 'roomlist', id: 'roomlist' });
  els.results = h('div', { class: 'roomlist', hidden: true });
  els.conn = h('div', { class: 'conn', hidden: true, role: 'status' }, '连接已断开，正在重连…');
  els.pill = h('button', { class: 'pill', type: 'button', title: 'Agent 状态', onclick: () => emit('open-settings', 'agent') });
  els.meBtn = h('button', { class: 'me', type: 'button', title: '账号与设置', onclick: () => emit('open-settings', 'account') });
  container.append(
    h('header', { class: 'left-head' }, els.meBtn, h('div', { class: 'grow' }), els.pill, iconBtn('plus', '新建聊天', openNewChat), iconBtn('gear', '设置', () => emit('open-settings'))),
    h('div', { class: 'searchbar' }, icon('search', 16), els.search),
    els.tabs,
    els.list,
    els.results,
    els.conn
  );
  els.search.addEventListener('input', debounce(runSearch, 300));
  els.search.addEventListener('keydown', (e) => e.key === 'Escape' && ((els.search.value = ''), runSearch()));
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      S.view = 'list';
      emit('view');
      els.search.focus();
    }
  });

  on('rooms:render', renderRooms);
  on('rooms:reload', loadRooms);
  on('room:update', (r) => {
    S.rooms.set(r.id, r);
    renderRooms();
  });
  on('room:removed', (id) => {
    S.rooms.delete(id);
    renderRooms();
  });
  on('msg:new', onNew);
  on('msg:update', (m) => {
    const r = room(m.room_id);
    if (r && r.last_message && r.last_message.id === m.id) {
      r.last_message = preview(m);
      renderRooms();
    }
  });
  on('read', (d) => {
    const r = room(d.room_id);
    if (r) {
      r.last_read_seq = d.last_read_seq;
      r.unread = d.unread;
      renderRooms();
    }
  });
  on('agent', renderPill);
  on('online', (v) => (els.conn.hidden = v));
  renderPill();
  renderMe();
}

export async function loadRooms() {
  const { rooms } = await get('/api/rooms');
  S.rooms = new Map(rooms.map((r) => [r.id, r]));
  renderRooms();
  if (S.cur && room(S.cur)) emit('room:update', room(S.cur));
}

export async function loadUsers() {
  S.users = (await get('/api/users')).users;
}

function preview(m) {
  return { id: m.id, seq: m.seq, sender_id: m.sender_id, kind: m.kind, body: m.deleted ? null : (m.body || '').slice(0, 120), deleted: m.deleted, has_attachment: !!m.attachment, created_at: m.created_at };
}

function onNew(m) {
  const r = room(m.room_id);
  if (!r) {
    loadRooms();
    return;
  }
  r.max_seq = Math.max(r.max_seq, m.seq);
  r.last_message = preview(m);
  r.last_activity = m.created_at;
  const viewing = m.room_id === S.cur && document.visibilityState === 'visible' && !S.msgs.get(m.room_id)?.hasAfter;
  if (m.sender_id !== S.me.id && !viewing) r.unread += 1;
  renderRooms();
}

export function renderMe() {
  if (!S.me) return;
  els.meBtn.replaceChildren(avatar(S.me, 34), h('span', { class: 'me-name' }, S.me.display_name));
}

export function renderPill() {
  const st = S.agent;
  if (!st) return;
  const text = { unconfigured: 'Agent 未配置', offline: 'Agent 离线', online: 'Agent 在线' }[st.state];
  els.pill.replaceChildren(h('span', { class: 'dot ' + st.state }), text);
  els.pill.title = st.state === 'online' ? `Agent 在线 · 待整理 ${st.backlog_events} 条` : 'Agent 状态与接入';
}

export function renderRooms() {
  if (!els.list) return;
  const all = [...S.rooms.values()];
  const active = all.filter((r) => !r.archived);
  const archived = all.filter((r) => r.archived);
  const unreadActive = active.reduce((n, r) => n + (r.muted ? 0 : r.unread), 0);
  els.tabs.replaceChildren(
    h('button', { class: 'tab' + (!S.showArchived ? ' on' : ''), role: 'tab', 'aria-selected': String(!S.showArchived), onclick: () => ((S.showArchived = false), renderRooms()) }, '聊天', unreadActive ? h('span', { class: 'badge' }, unreadActive) : null),
    h('button', { class: 'tab' + (S.showArchived ? ' on' : ''), role: 'tab', 'aria-selected': String(S.showArchived), onclick: () => ((S.showArchived = true), renderRooms()) }, '已归档', archived.length ? h('span', { class: 'count' }, archived.length) : null)
  );
  const list = (S.showArchived ? archived : active).sort((a, b) => (b.last_activity || '').localeCompare(a.last_activity || ''));
  els.list.replaceChildren(...(list.length ? list.map(roomItem) : [h('div', { class: 'empty' }, S.showArchived ? '没有已归档的聊天' : '还没有聊天，点右上角 ＋ 开始')]));
  document.title = (unreadActive ? `(${unreadActive}) ` : '') + 'SophosChat';
  emit('unread', unreadActive);
}

function roomItem(r) {
  const lm = r.last_message;
  let text = '暂无消息';
  if (lm) {
    const body = lm.deleted ? '消息已删除' : lm.body || (lm.has_attachment ? '[附件]' : '');
    const who = lm.sender_id === S.me.id ? '我：' : r.type === 'group' || lm.kind === 'agent' ? userById(lm.sender_id).display_name + '：' : '';
    text = who + body.replace(/\s+/g, ' ');
  }
  const o = r.type === 'dm' ? otherHuman(r) : r.type === 'agent_dm' ? agentUser(r) : { id: r.id, display_name: roomTitle(r), role: 'user' };
  return h(
    'button',
    { class: 'room-item' + (r.id === S.cur ? ' on' : ''), type: 'button', 'aria-current': r.id === S.cur ? 'true' : null, onclick: () => emit('open-room', { id: r.id }) },
    avatar(o || { id: r.id, display_name: roomTitle(r) }, 44),
    h(
      'div',
      { class: 'ri-main' },
      h('div', { class: 'ri-top' }, h('span', { class: 'ri-title' }, roomTitle(r), r.muted ? h('span', { class: 'muted-ico', title: '已静音' }, '🔕') : null), h('time', null, fmtRel(lm ? lm.created_at : r.created_at))),
      h('div', { class: 'ri-bottom' }, h('span', { class: 'ri-text' }, text), r.unread ? h('span', { class: 'badge' + (r.muted ? ' mute' : '') }, r.unread > 99 ? '99+' : r.unread) : null)
    )
  );
}

// ------------------------------------------------------------------ 全局搜索
async function runSearch() {
  const q = els.search.value.trim();
  if (!q) {
    els.results.hidden = true;
    els.list.hidden = false;
    els.tabs.hidden = false;
    return;
  }
  els.results.hidden = false;
  els.list.hidden = true;
  els.tabs.hidden = true;
  els.results.replaceChildren(h('div', { class: 'empty' }, '搜索中…'));
  try {
    const { messages } = await get('/api/search' + qs({ q, limit: 50 }));
    els.results.replaceChildren(
      ...(messages.length
        ? messages.map((m) => {
            const r = room(m.room_id);
            return h(
              'button',
              { class: 'result', type: 'button', onclick: () => emit('jump', { room_id: m.room_id, seq: m.seq, id: m.id }) },
              h('div', { class: 'r-top' }, h('b', null, r ? roomTitle(r) : '会话'), h('time', null, `${fmtDay(m.created_at)} ${fmtTime(m.created_at)}`)),
              h('div', { class: 'r-body' }, userById(m.sender_id).display_name + '：' + (m.body || '[附件]'))
            );
          })
        : [h('div', { class: 'empty' }, '没有找到相关消息')])
    );
  } catch (e) {
    els.results.replaceChildren(h('div', { class: 'empty' }, e.message));
  }
}

// ------------------------------------------------------------------ 新建聊天
export function openNewChat() {
  let mode = 'dm';
  const body = h('div', { class: 'newchat' });
  const withAgent = h('input', { type: 'checkbox', checked: true });
  withAgent.checked = !!S.agent;
  const title = h('input', { type: 'text', placeholder: '群名称', maxlength: '60', 'aria-label': '群名称' });
  const picks = new Set();
  const render = () => {
    body.replaceChildren(
      h(
        'div',
        { class: 'seg', role: 'tablist' },
        h('button', { type: 'button', class: mode === 'dm' ? 'on' : '', onclick: () => ((mode = 'dm'), render()) }, '双人私聊'),
        h('button', { type: 'button', class: mode === 'group' ? 'on' : '', onclick: () => ((mode = 'group'), render()) }, '群聊')
      ),
      mode === 'group' ? title : null,
      h('div', { class: 'pick-list' }, S.users.length ? S.users.map((u) => userRow(u)) : h('div', { class: 'empty' }, '还没有其他用户。请 Owner 在 设置 → 成员 里添加。')),
      h('label', { class: 'check' }, withAgent, h('span', null, '让 Agent 加入这个聊天', h('small', null, 'Agent 默认不打扰，只在被 @ 时回复；并会在后台静默整理主题记忆。可随时在会话设置里关闭。')))
    );
  };
  const userRow = (u) => {
    const id = 'pk' + u.id;
    const input = h('input', { type: mode === 'dm' ? 'radio' : 'checkbox', name: 'pick', id, checked: picks.has(u.id) || null });
    input.checked = picks.has(u.id);
    input.addEventListener('change', () => {
      if (mode === 'dm') picks.clear();
      input.checked ? picks.add(u.id) : picks.delete(u.id);
    });
    return h('label', { class: 'pick', for: id }, input, avatar(u, 30), h('span', null, u.display_name), h('small', null, '@' + u.username));
  };
  render();
  const create = async () => {
    if (!picks.size) return toast('请选择聊天对象', 'err');
    try {
      let res;
      if (mode === 'dm') res = await post('/api/rooms', { type: 'dm', user_id: [...picks][0], include_agent: withAgent.checked });
      else {
        if (!title.value.trim()) return toast('请填写群名称', 'err');
        res = await post('/api/rooms', { type: 'group', title: title.value.trim(), member_ids: [...picks], include_agent: withAgent.checked });
      }
      S.rooms.set(res.room.id, res.room);
      close();
      renderRooms();
      emit('open-room', { id: res.room.id });
    } catch (e) {
      toast(e.message, 'err');
    }
  };
  const close = modal({ title: '新建聊天', body, actions: [h('button', { class: 'btn', type: 'button', onclick: () => close() }, '取消'), h('button', { class: 'btn primary', type: 'button', onclick: create }, '开始聊天')] });
}
