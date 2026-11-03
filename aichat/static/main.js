import { get, post, ApiError } from './api.js';
import { S, emit, on, room, roomTitle } from './store.js';
import { $, h, toast, avatar, showTimes, setShowTimes } from './util.js';
import { connect, disconnect } from './ws.js';
import { initRooms, loadRooms, loadUsers, renderMe } from './rooms.js';
import { initChat, openRoom } from './chatview.js';
import { initPanel, refresh as refreshPanel } from './panel.js';
import { openSettings } from './settings.js';
import { initCalls } from './call.js';
import { applyPrefs, savePrefs } from './appearance.js';
import { initNav, syncView } from './nav.js';
import { setVoiceRate, setAutoNext } from './voice.js';

const root = $('#root');
setShowTimes(showTimes()); // 套用“是否显示每条消息时间”的偏好
let built = false;
let app;

// 返回键：手机上“聊天 / 回顾面板”各算一层；按返回先退一层，不会直接回桌面
initNav({
  onBackView: () => {
    S.view = S.view === 'panel' ? 'chat' : 'list';
    emit('view');
  },
});
const VIEW_DEPTH = { list: 0, chat: 1, panel: 2 };

function applyLayout() {
  if (!app) return;
  app.dataset.view = S.view;
  app.dataset.panel = S.panelOpen ? 'open' : 'closed';
  syncView(window.innerWidth < 900 ? VIEW_DEPTH[S.view] || 0 : 0);
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
  let registering = false;
  const display = h('input', { type:'text', name:'display_name', placeholder:'昵称（可选）', 'aria-label':'昵称', maxlength:40, hidden:true });
  const confirm = h('input', { type:'password', name:'confirm_password', autocomplete:'new-password', placeholder:'再次输入密码', 'aria-label':'确认密码', hidden:true, oninput:()=>{err.textContent='';} });
  const mode = h('button', { type:'button', class:'btn wide', hidden:true, onclick:()=>{
    registering=!registering;display.hidden=confirm.hidden=!registering;confirm.required=registering;
    pass.autocomplete=registering?'new-password':'current-password';pass.minLength=registering?8:1;
    btn.textContent=registering?'注册并登录':'登录';mode.textContent=registering?'已有账号，返回登录':'没有账号？注册';err.textContent='';
  }},'没有账号？注册');
  // 邀请链接：/?invite=xxx —— 显示邀请人并直接进入注册
  const invite = new URLSearchParams(location.search).get('invite');
  const inviteNote = h('p', { class: 'invite-note', hidden: true });
  if (invite) {
    get('/api/invite-info' + '?token=' + encodeURIComponent(invite)).then((r) => {
      if (r.valid) {
        inviteNote.textContent = `${r.inviter} 邀请你加入 SophosChat` + (r.room_title ? `，并进入「${r.room_title}」` : '') + '。设置用户名和密码即可开始。';
        inviteNote.hidden = false;
        if (!registering) mode.click();
        mode.hidden = false;
      } else {
        inviteNote.textContent = { expired: '这个邀请已过期', used_up: '这个邀请已被用完', revoked: '这个邀请已被撤销' }[r.reason] || '邀请链接无效';
        inviteNote.textContent += '，请向邀请人重新索取。已有账号可以直接登录。';
        inviteNote.classList.add('bad');
        inviteNote.hidden = false;
      }
    }).catch(() => {});
  }
  get('/api/registration').then(r=>{if(!invite)mode.hidden=!r.enabled;}).catch(()=>{});
  const form = h(
    'form',
    {
      class: 'login-card',
      onsubmit: async (e) => {
        e.preventDefault();
        btn.disabled = true;
        err.textContent = '';
        try {
          if(registering && pass.value!==confirm.value) throw new Error('两次密码不一致');
          await post(registering?'/api/register':'/api/login', { username: user.value.trim(), password: pass.value, ...(registering?{display_name:display.value.trim(), ...(invite?{invite}:{})}: {}) }, { quiet401: true });
          if (invite) history.replaceState(null, '', '/');
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
    h('p', { class: 'muted' }, '私人聊天，AI 在需要时才出现。'),
    inviteNote,
    user,
    display,
    pass,
    confirm,
    err,
    btn,
    mode
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
  if (me.prefs) {
    applyPrefs(me.prefs);
    setVoiceRate(me.prefs.voice_rate, (r) => savePrefs({ voice_rate: r }));
    setAutoNext(me.prefs.autoplay);
  }
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
