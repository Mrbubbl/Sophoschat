import { get, post, ApiError } from './api.js';
import { S, emit, on, room, roomTitle } from './store.js';
import { $, h, toast, avatar, showTimes, setShowTimes } from './util.js';
import { connect, disconnect } from './ws.js';
import { initRooms, loadRooms, loadUsers, renderMe } from './rooms.js';
import { initChat, openRoom } from './chatview.js';
import { initPanel, refresh as refreshPanel } from './panel.js';
import { openSettings } from './settings.js';
import { initCalls } from './call.js';

const root = $('#root');
setShowTimes(showTimes()); // 套用“是否显示每条消息时间”的偏好
let built = false;
let app;

function applyLayout() {
  if (!app) return;
  app.dataset.view = S.view;
  app.dataset.panel = S.panelOpen ? 'open' : 'closed';
}

function buildShell() {
  if (built) return;
  built = true;
  const left = h('aside', { id: 'left', class: 'left' });
  const center = h('main', { id: 'center', class: 'center' });
  const panel = h('aside', { id: 'panel', class: 'panel' });
  app = h('div', { id: 'app', dataset: { view: 'list', panel: 'closed' } }, left, center, panel);
  root.replaceChildren(app);
  initRooms(left);
  initChat(center);
  initPanel(panel);
  initCalls();

  on('view', applyLayout);
  on('toggle-panel', () => {
    if (window.innerWidth < 900) S.view = S.view === 'panel' ? 'chat' : 'panel';
    else S.panelOpen = !S.panelOpen;
    applyLayout();
    if (S.panelOpen || S.view === 'panel') refreshPanel();
  });
  on('open-settings', (tab) => openSettings(typeof tab === 'string' ? tab : 'account'));
  on('open-room', async (d) => {
    S.view = 'chat';
    applyLayout();
  });
  on('user:updated', (u) => {
    if (S.me && u.id === S.me.id) S.me = { ...S.me, ...u };
    if (Array.isArray(S.users)) S.users = S.users.map((x) => (x.id === u.id ? { ...x, ...u } : x));
    for (const r of S.rooms.values()) for (const m of r.members || []) if (m.id === u.id) Object.assign(m, u);
    document.querySelectorAll(`.avatar[data-uid="${u.id}"]`).forEach((el) => el.replaceWith(avatar(u, Number(el.dataset.size) || 36)));
  });
  on('msg:new', notify);
  let badgeTimer = 0;
  const updateBadge = () => {
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(() => {
      if (!navigator.setAppBadge) return;
      const n = [...S.rooms.values()].filter((r) => !r.muted && !r.archived).reduce((a, r) => a + (r.unread || 0), 0);
      (n > 0 ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});
    }, 300);
  };
  on('rooms:render', updateBadge);
  on('read', updateBadge);
  on('room:update', updateBadge);
  on('unauthenticated', showLogin);
  window.addEventListener('resize', () => {
    if (window.innerWidth >= 900 && S.view === 'panel') S.view = 'chat';
    applyLayout();
  });
  applyLayout();
  fitViewport();
}

// iOS Safari 弹出键盘时不会缩小布局视口：用 visualViewport 把应用高度钉在可见区域，输入框才不会被顶出屏幕
function fitViewport() {
  const vv = window.visualViewport;
  const root = document.documentElement;
  const fit = () => {
    root.style.setProperty('--app-h', (vv ? vv.height : window.innerHeight) + 'px');
    root.style.setProperty('--app-top', (vv ? vv.offsetTop : 0) + 'px');
    if (!vv || vv.offsetTop) window.scrollTo(0, 0);
    document.dispatchEvent(new Event('viewport-fit'));
  };
  if (vv) {
    vv.addEventListener('resize', fit);
    vv.addEventListener('scroll', fit);
  }
  window.addEventListener('orientationchange', () => setTimeout(fit, 250));
  fit();
}

function notify(m) {
  if (m.sender_id === S.me.id || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  const r = room(m.room_id);
  if (!r || r.muted || (document.visibilityState === 'visible' && m.room_id === S.cur)) return;
  const n = new Notification(roomTitle(r), { body: m.deleted ? '' : (m.body || '[附件]').slice(0, 100), tag: 'room-' + r.id, icon: '/static/icon-192.png' });
  n.onclick = () => {
    window.focus();
    emit('open-room', { id: r.id });
    n.close();
  };
}

// ------------------------------------------------------------------ 登录
function showLogin(msg) {
  disconnect();
  S.cur = null;
  S.msgs.clear();
  S.tasks.clear();
  built = false;
  app = null;
  const user = h('input', { type: 'text', name: 'username', autocomplete: 'username', placeholder: '用户名', required: true, 'aria-label': '用户名', autofocus: true });
  const pass = h('input', { type: 'password', name: 'password', autocomplete: 'current-password', placeholder: '密码', required: true, 'aria-label': '密码' });
  const err = h('div', { class: 'login-err', role: 'alert' }, typeof msg === 'string' ? msg : '');
  const btn = h('button', { class: 'btn primary wide', type: 'submit' }, '登录');
  const form = h(
    'form',
    {
      class: 'login-card',
      onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.textContent = '';
        try {
          await post('/api/login', { username: user.value.trim(), password: pass.value }, { quiet401: true });
          await start();
        } catch (ex) {
          err.textContent = ex.message;
          btn.disabled = false;
          pass.select();
        }
      },
    },
    h('div', { class: 'login-logo' }, h('img', { class: 'logo-img', src: '/static/icon.svg', alt: 'SophosChat' })),
    h('h1', null, 'SophosChat'),
    h('p', { class: 'muted' }, '自部署的私人聊天，Agent 默认不打扰，只在需要时出现。'),
    user,
    pass,
    err,
    btn
  );
  root.replaceChildren(h('div', { class: 'login' }, form));
  user.focus();
}

async function start() {
  let me;
  try {
    me = await get('/api/me', { quiet401: true });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return showLogin();
    root.replaceChildren(h('div', { class: 'login' }, h('div', { class: 'login-card' }, h('h1', null, '无法连接服务'), h('p', { class: 'muted' }, e.message), h('button', { class: 'btn primary', onclick: () => location.reload() }, '重试'))));
    return;
  }
  S.me = me.user;
  S.agent = me.agent;
  buildShell();
  renderMe();
  await Promise.all([loadUsers(), loadRooms()]);
  S.panelOpen = window.innerWidth >= 1180;
  applyLayout();
  connect();
  emit('agent', S.agent);
  const first = [...S.rooms.values()].filter((r) => !r.archived).sort((a, b) => (b.last_activity || '').localeCompare(a.last_activity || ''))[0];
  if (first && window.innerWidth >= 900) await openRoom(first.id);
  refreshPanel();
  const want = Number(new URLSearchParams(location.search).get('room'));
  if (want && S.rooms.has(want)) {
    history.replaceState(null, '', '/');
    emit('open-room', { id: want });
  }
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
  // 点通知 → 打开对应会话
  navigator.serviceWorker.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'open-room' && e.data.room_id) emit('open-room', { id: e.data.room_id });
  });
}
start();
