// 中间的聊天视图：消息流、编辑/回复/收藏/删除、@Agent、附件、任务状态、历史分页与跳转
import { api, get, post, patch, del, qs } from './api.js';
import { S, emit, on, room, roomTitle, roomSubtitle, otherHuman, agentUser, userById, agentStateText } from './store.js';
import { buildEmojiPanel, QUICK } from './emoji.js';
import { voiceSupported, startRecording, voicePlayer, recordingBar, MAX_MS } from './voice.js';
import { wsSend } from './ws.js';
import { canCall, startCall } from './call.js';
import { showTimes, $, h, icon, iconBtn, avatar, renderMarkdown, fmtTime, fmtFull, fmtDay, dayKey, fmtSize, toast, ask, copyText, debounce } from './util.js';

const els = {};
const drafts = new Map(); // room -> { text, reply }
let replyTo = null;
let pendingAtt = null;
let uploading = false;
let unreadMarker = null;
let dividerId = null;
let newCount = 0;
let loadingOlder = false;
let loadingNewer = false;
const coarse = matchMedia('(pointer: coarse)').matches;

// ------------------------------------------------------------------ 初始化
export function initChat(container) {
  els.empty = h('div', { class: 'empty-center' }, h('div', { class: 'empty-art' }, icon('bot', 40)), h('h2', null, 'SophosChat'), h('p', null, '选择一个聊天，或点左上角 ＋ 新建'));
  els.head = h('header', { class: 'chat-head' });
  els.msgs = h('div', { class: 'msgs', id: 'msgs', tabindex: '0', role: 'log', 'aria-live': 'polite', 'aria-label': '消息列表' });
  els.toBottom = h('button', { class: 'to-bottom', type: 'button', hidden: true, onclick: () => (S.msgs.get(S.cur)?.hasAfter ? goLatest() : scrollToBottom(true)) });
  els.searchPane = h('div', { class: 'searchpane', hidden: true });
  els.drop = h('div', { class: 'dropzone', hidden: true }, '松开鼠标，上传文件');
  els.wrap = h('div', { class: 'msgwrap' }, els.msgs, els.toBottom, els.searchPane, els.drop);
  els.agentbar = h('div', { class: 'agentbar', hidden: true });
  els.replybar = h('div', { class: 'replybar', hidden: true });
  els.attbar = h('div', { class: 'attbar', hidden: true });
  els.mention = h('div', { class: 'mention-pop', role: 'listbox', hidden: true });
  els.input = h('textarea', { id: 'input', rows: '1', enterkeyhint: 'enter', autocomplete: 'off', autocapitalize: 'sentences', 'aria-label': '消息输入框', maxlength: '20000' });
  els.file = h('input', { type: 'file', hidden: true, onchange: () => els.file.files[0] && uploadFile(els.file.files[0]) });
  els.imgFile = h('input', { type: 'file', accept: 'image/*', hidden: true, onchange: () => els.imgFile.files[0] && uploadFile(els.imgFile.files[0]) });
  els.camFile = h('input', { type: 'file', accept: 'image/*', capture: 'environment', hidden: true, onchange: () => els.camFile.files[0] && uploadFile(els.camFile.files[0]) });
  els.send = h('button', { class: 'send', type: 'button', title: '发送', 'aria-label': '发送', disabled: true, onclick: onSendClick }, icon('send', 20));
  els.send.addEventListener('pointerdown', (e) => e.preventDefault());
  els.send.addEventListener('mousedown', (e) => e.preventDefault());
  els.imgBtn = iconBtn('image', coarse ? '相册（长按拍照）' : '从相册选择图片', () => {}, 'narrowbtn');
  els.imgBtn.onclick = () => (els.imgHeld ? (els.imgHeld = false) : els.imgFile.click());
  if (coarse) {
    let t = 0;
    const stop = () => clearTimeout(t);
    els.imgBtn.addEventListener('pointerdown', () => {
      els.imgHeld = false;
      t = setTimeout(() => {
        els.imgHeld = true;
        navigator.vibrate && navigator.vibrate(12);
        els.camFile.click(); // 长按直接打开相机
      }, 450);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((ev) => els.imgBtn.addEventListener(ev, stop));
    els.imgBtn.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  els.atBtn = iconBtn('at', '提及 Agent', () => insertMention(agentName()), 'inbox hide-narrow');
  els.emojiBtn = iconBtn('smile', '表情', () => toggleEmoji(), 'inbox');
  els.emojiBtn.addEventListener('pointerdown', (e) => e.preventDefault());
  els.cbox = h('div', { class: 'cbox' }, els.input, els.atBtn, els.emojiBtn);
  els.emojiHolder = h('div', { class: 'emoji-holder', hidden: true });
  els.rowMain = h('div', { class: 'composer-row' }, els.imgBtn, iconBtn('clip', '添加文件', () => els.file.click()), els.cbox, els.send);
  els.composer = h('footer', { class: 'composer' }, els.replybar, els.attbar, els.mention, els.rowMain, els.emojiHolder, els.file, els.imgFile, els.camFile);
  els.typing = h('div', { class: 'typingbar', hidden: true });
  els.wrap.append(els.typing);
  els.chat = h('section', { class: 'chat', hidden: true }, els.head, els.wrap, els.agentbar, els.composer);
  container.append(els.empty, els.chat);

  els.input.addEventListener('input', () => {
    autosize();
    saveDraft();
    checkMention();
    sendTyping();
  });
  els.input.addEventListener('focus', () => hideEmoji());
  els.input.addEventListener('keydown', onKeydown);
  document.addEventListener('viewport-fit', () => document.activeElement === els.input && scrollToBottom(false));
  els.input.addEventListener('paste', (e) => {
    const f = [...(e.clipboardData?.files || [])][0];
    if (f) {
      e.preventDefault();
      uploadFile(f);
    }
  });
  els.input.addEventListener('blur', () => setTimeout(() => (els.mention.hidden = true), 150));
  els.msgs.addEventListener('scroll', debounce(onScroll, 60));
  els.wrap.addEventListener('dragover', (e) => {
    e.preventDefault();
    els.drop.hidden = false;
  });
  els.wrap.addEventListener('dragleave', (e) => e.target === els.drop && (els.drop.hidden = true));
  els.wrap.addEventListener('drop', (e) => {
    e.preventDefault();
    els.drop.hidden = true;
    const f = e.dataTransfer?.files?.[0];
    if (f) uploadFile(f);
  });
  window.addEventListener('online', retryFailed);
  setInterval(() => navigator.onLine && retryFailed(), 8000);
  document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && markReadSoon());
  window.addEventListener('focus', markReadSoon);

  on('msg:new', onNewMessage);
  on('msg:update', onUpdateMessage);
  on('typing', onTyping);
  on('reaction', onReaction);
  on('task', onTask);
  on('agent', () => {
    refreshAgentUi();
    if (S.cur) $$chips();
  });
  on('room:update', (r) => r.id === S.cur && (renderHead(), refreshAgentUi()));
  on('room:removed', (id) => id === S.cur && closeRoom());
  on('resync', resync);
  on('jump', jumpTo);
  on('open-room', (d) => openRoom(d.id, d));
  on('online', () => S.cur && renderHead());
}

const $$chips = () => {
  const st = S.msgs.get(S.cur);
  if (!st) return;
  for (const m of st.list) refreshChip(m);
};

function agentName() {
  const r = room(S.cur);
  const a = r && agentUser(r);
  return a ? a.username : 'agent';
}

// ------------------------------------------------------------------ 打开 / 关闭会话
export async function openRoom(id, opts = {}) {
  if (S.cur && els.input) saveDraft();
  const r = room(id);
  if (!r) return;
  S.cur = id;
  S.view = 'chat';
  els.empty.hidden = true;
  els.chat.hidden = false;
  els.searchPane.hidden = true;
  unreadMarker = r.unread > 0 ? r.last_read_seq : null;
  dividerId = null;
  newCount = 0;
  replyTo = null;
  pendingAtt = null;
  renderHead();
  restoreDraft();
  refreshAgentUi();
  emit('view');
  autosize();
  emit('rooms:render');
  emit('room:opened', id);
  await loadInitial(id, { jumpSeq: opts.jumpSeq, fromTime: opts.fromTime });
  loadTasks(id);
}

function closeRoom() {
  S.cur = null;
  els.chat.hidden = true;
  els.empty.hidden = false;
  S.view = 'list';
  emit('view');
  emit('rooms:render');
}

async function loadInitial(id, { jumpSeq, fromTime } = {}) {
  els.msgs.replaceChildren(h('div', { class: 'loading' }, '加载中…'));
  const params = jumpSeq ? { around_seq: jumpSeq, limit: 40 } : fromTime ? { from_time: fromTime, limit: 40 } : { limit: 50 };
  let d;
  try {
    d = await get(`/api/rooms/${id}/messages${qs(params)}`);
  } catch (e) {
    els.msgs.replaceChildren(h('div', { class: 'loading err' }, '加载失败：' + e.message, h('button', { class: 'btn', onclick: () => loadInitial(id, { jumpSeq, fromTime }) }, '重试')));
    return;
  }
  if (S.cur !== id) return;
  S.msgs.set(id, { list: d.messages, hasBefore: d.has_more_before, hasAfter: d.has_more_after });
  renderAll();
  if (jumpSeq) scrollToSeq(jumpSeq);
  else if (fromTime) els.msgs.scrollTop = 0;
  else if (dividerId) {
    const el = els.msgs.querySelector('.unread-divider');
    el ? el.scrollIntoView({ block: 'center' }) : scrollToBottom(false);
  } else scrollToBottom(false);
  updateToBottom();
  markReadSoon();
}

async function loadTasks(id) {
  try {
    const { tasks } = await get(`/api/rooms/${id}/tasks`);
    for (const t of tasks.slice().reverse()) if (t.trigger_message_id) S.tasks.set(t.trigger_message_id, t);
    if (S.cur === id) $$chips();
  } catch {}
}

async function goLatest() {
  await loadInitial(S.cur, {});
  scrollToBottom(true);
}

// ------------------------------------------------------------------ 头部
function renderHead() {
  const r = room(S.cur);
  if (!r) return;
  const o = r.type === 'dm' ? otherHuman(r) : r.type === 'agent_dm' ? agentUser(r) : { id: r.id, display_name: roomTitle(r), role: 'user' };
  const dateInput = h('input', { type: 'date', class: 'date-input', 'aria-label': '跳转到日期', max: new Date().toISOString().slice(0, 10), onchange: () => dateInput.value && jumpDate(dateInput.value) });
  els.head.replaceChildren(
    iconBtn('back', '返回聊天列表', () => ((S.view = 'list'), emit('view')), 'only-narrow'),
    avatar(o || { id: r.id, display_name: roomTitle(r) }, 38),
    h('div', { class: 'head-title' }, h('h1', null, roomTitle(r)), h('div', { class: 'sub' }, roomSubtitle(r), !S.online ? h('span', { class: 'offline-tag' }, ' · 连接中断') : null)),
    h('div', { class: 'grow' }),
    h(
      'div',
      { class: 'head-actions' },
      h(
        'div',
        { class: 'acts-list', onclick: () => setTimeout(closeHeadActions, 0) },
        canCall(r) ? iconBtn('phone', '语音通话', () => startCall(r.id, 'audio')) : null,
        canCall(r) ? iconBtn('video', '视频通话', () => startCall(r.id, 'video')) : null,
        r.agent_in_room ? iconBtn('archive', '总结并存档', () => summarizeAndArchive()) : null,
        iconBtn('search', '在本会话中搜索', toggleSearch),
        h('span', { class: 'date-wrap' }, iconBtn('calendar', '按日期跳转', () => (dateInput.showPicker ? dateInput.showPicker() : dateInput.click())), dateInput),
        iconBtn('panel', '回顾与主题记忆', () => emit('toggle-panel'))
      ),
      iconBtn('more', '更多操作', toggleHeadActions, 'act-toggle')
    )
  );
  els.atBtn.hidden = !(r.agent_in_room && r.type !== 'agent_dm');
}

// 手机上：顶栏的操作按钮平时收在最右边（一个“⋯”），点开后才展开，不再挡住头像和名字
let headActionsTimer = 0;
function closeHeadActions() {
  clearTimeout(headActionsTimer);
  els.head?.querySelector('.head-actions')?.classList.remove('open');
}
function toggleHeadActions(e) {
  const box = els.head.querySelector('.head-actions');
  const open = !box.classList.contains('open');
  box.classList.toggle('open', open);
  clearTimeout(headActionsTimer);
  if (open) headActionsTimer = setTimeout(closeHeadActions, 6000);
  e && e.stopPropagation();
}
document.addEventListener('pointerdown', (e) => {
  if (!e.target.closest('.head-actions')) closeHeadActions();
});

// 一键总结并存档：给 Agent 发一条固定指令；Agent 回复纪要（配置了 Notion 时同时存档并附上链接）
async function summarizeAndArchive() {
  if (!S.cur) return;
  if (!(await ask('让 Agent 总结最近的讨论并存档？它会在聊天里回复纪要。', { ok: '总结' }))) return;
  const keep = els.input.value;
  els.input.value = `@${agentName()} /总结存档`;
  await send();
  els.input.value = keep;
  autosize();
  saveDraft();
}

async function jumpDate(dateStr) {
  const local = new Date(dateStr + 'T00:00:00');
  await loadInitial(S.cur, { fromTime: local.toISOString() });
}

function refreshAgentUi() {
  const r = room(S.cur);
  if (!r) return;
  const st = S.agent;
  if (r.agent_in_room && st && st.state !== 'online') {
    els.agentbar.hidden = false;
    els.agentbar.replaceChildren(
      h('span', { class: 'dot ' + st.state }),
      h('span', { class: 'abtext' }, (st.state === 'unconfigured' ? 'Agent 还没有接入：' : 'Agent 当前离线：') + '消息会照常保存，Agent 上线后才会处理。'),
      h('button', { class: 'link', type: 'button', onclick: () => emit('open-settings', 'agent') }, st.state === 'unconfigured' ? '去接入' : '查看状态')
    );
  } else els.agentbar.hidden = true;
}

// ------------------------------------------------------------------ 渲染消息
function visibleList() {
  return S.msgs.get(S.cur)?.list || [];
}

function renderAll() {
  const st = S.msgs.get(S.cur);
  els.msgs.replaceChildren();
  if (!st) return;
  if (st.hasBefore) els.msgs.append(h('div', { class: 'older' }, '向上滚动加载更早的消息'));
  else if (st.list.length) els.msgs.append(h('div', { class: 'older' }, '— 已到最早的消息 —'));
  else els.msgs.append(h('div', { class: 'older' }, '还没有消息，打个招呼吧'));
  if (dividerId === null && unreadMarker !== null) {
    const first = st.list.find((m) => m.seq > unreadMarker && m.sender_id !== S.me.id && !m.local);
    dividerId = first ? first.id : null;
  }
  let prev = null;
  for (const m of st.list) {
    els.msgs.append(...rowEls(m, prev));
    prev = m;
  }
}

function rowEls(m, prev) {
  const out = [];
  if (!prev || dayKey(prev.created_at) !== dayKey(m.created_at)) out.push(h('div', { class: 'day-sep' }, h('span', null, fmtDay(m.created_at))));
  // 不显示每条时间时，像微信一样：间隔超过 5 分钟才插一条居中的时间
  else if (!showTimes() && prev && new Date(m.created_at) - new Date(prev.created_at) >= 5 * 60000) out.push(h('div', { class: 'time-sep' }, h('span', null, fmtTime(m.created_at))));
  if (dividerId !== null && m.id === dividerId) out.push(h('div', { class: 'unread-divider' }, h('span', null, '以下为未读消息')));
  out.push(messageRow(m, prev));
  return out;
}

function messageRow(m, prev) {
  const r = room(S.cur);
  const mine = m.sender_id === S.me.id && m.kind === 'human';
  const isAgent = m.kind === 'agent';
  const sender = userById(m.sender_id);
  const grouped = prev && prev.sender_id === m.sender_id && prev.kind === m.kind && dayKey(prev.created_at) === dayKey(m.created_at) && new Date(m.created_at) - new Date(prev.created_at) < 5 * 60000;
  const showName = !mine && (r.type === 'group' || isAgent) && !grouped;
  const row = h('div', { class: 'row ' + (mine ? 'own' : 'other') + (isAgent ? ' agent' : '') + (m.local ? ' pending' : '') + (grouped ? ' grouped' : ''), dataset: { mid: m.id } });
  if (!mine) row.append(grouped ? h('div', { class: 'avatar-gap' }) : avatar(sender, 32));
  const bubble = h('div', { class: 'bubble' + (m.deleted ? ' deleted' : '') });
  if (showName) bubble.append(h('div', { class: 'sender' }, isAgent && sender.display_name.toLowerCase() === 'agent' ? null : sender.display_name, isAgent ? h('span', { class: 'agent-badge' }, 'Agent') : null));
  if (m.deleted) {
    bubble.append(h('div', { class: 'body' }, '此消息已删除'));
  } else {
    if (m.reply_to && m.reply_preview) {
      const rp = m.reply_preview;
      bubble.append(
        h(
          'button',
          { class: 'reply', type: 'button', onclick: () => emit('jump', { room_id: m.room_id, seq: rp.seq, id: rp.id }) },
          h('b', null, userById(rp.sender_id).display_name),
          h('span', null, rp.deleted ? '消息已删除' : rp.body || (rp.has_attachment ? '[附件]' : ''))
        )
      );
    }
    if (m.body) bubble.append(h('div', { class: 'body' }, renderMarkdown(m.body, { members: r.members })));
    if (m.attachment) bubble.append(attachmentEl(m.attachment));
  }
  const BLOCK = new Set(['DIV', 'P', 'UL', 'OL', 'LI', 'BLOCKQUOTE', 'H1', 'H2', 'H3', 'H4']);
  const tailBlock = (el) => {
    for (;;) {
      const last = el.lastElementChild;
      if (!last) return el;
      if (['PRE', 'TABLE', 'HR'].includes(last.tagName)) return null;
      if (BLOCK.has(last.tagName)) el = last;
      else return el;
    }
  };
  // 时间显示在文字最后一行的右侧，不再单独占一行；代码块结尾、发送中/失败等情况仍放在下面
  const bodyEl = !m.deleted && !m.local && m.body ? bubble.querySelector(':scope > .body') : null;
  const slot = bodyEl ? tailBlock(bodyEl) : null;
  if (slot) {
    slot.append(h('span', { class: 'tpad' + (m.edited_at || m.starred ? ' wide' : '') }));
    bubble.classList.add('tinline');
  }
  const meta = h('div', { class: 'meta' });
  if (m.edited_at && !m.deleted) meta.append(h('span', { class: 'edited' }, '已编辑'));
  if (m.starred) meta.append(h('span', { class: 'starred', title: '已收藏' }, icon('star', 12)));
  if (m.local) {
    meta.append(
      m.failed
        ? h('span', { class: 'fail' }, '发送失败 ', h('button', { class: 'link', onclick: () => retrySend(m) }, '重试'), ' ', h('button', { class: 'link', onclick: () => discardPending(m) }, '丢弃'))
        : h('span', { class: 'sending' }, '发送中…')
    );
  } else meta.append(h('time', { datetime: m.created_at, title: m.created_at }, fmtTime(m.created_at)));
  bubble.append(meta);
  const wrap = h('div', { class: 'bwrap' }, bubble);
  if (!m.local && !m.deleted) {
    attachGestures(bubble, row, m);
    if (!coarse) {
      wrap.append(
        h(
          'div',
          { class: 'tools' },
          h('button', { class: 'tool', type: 'button', title: '引用', 'aria-label': '引用这条消息', onclick: () => (setReply(m), els.input.focus()) }, icon('reply', 15)),
          h('button', { class: 'tool', type: 'button', title: '更多', 'aria-label': '消息操作', onclick: (e) => openMenu(e.currentTarget, m) }, icon('more', 15))
        )
      );
    }
  }
  row.append(wrap);
  const rx = !m.deleted && !m.local ? reactionsEl(m) : null;
  if (rx) wrap.append(rx);
  const chip = taskChip(m);
  if (chip) wrap.append(chip);
  return row;
}

function attachmentEl(a) {
  if (a.is_voice) return voicePlayer(a);
  if (a.is_image) {
    return h('img', { class: 'att-img', src: a.url, alt: a.filename, loading: 'lazy', onclick: () => lightbox(a), onload: () => nearBottomStick() });
  }
  return h('a', { class: 'att-file', href: a.url + '?download=1', download: a.filename }, icon('download', 16), h('span', { class: 'fname' }, a.filename), h('small', null, fmtSize(a.size)));
}

function lightbox(a) {
  const close = () => ov.remove();
  const ov = h('div', { class: 'lightbox', onclick: close }, h('img', { src: a.url, alt: a.filename }), h('button', { class: 'lb-close', type: 'button', 'aria-label': '关闭', onclick: close }, icon('close', 22)));
  document.body.append(ov);
  const k = (e) => e.key === 'Escape' && (close(), document.removeEventListener('keydown', k));
  document.addEventListener('keydown', k);
}

// ------------------------------------------------------------------ 任务状态
function taskChip(m) {
  const t = S.tasks.get(m.id);
  const r = room(S.cur);
  if (!t || m.deleted || !r || !r.agent_in_room) return null;
  const st = S.agent;
  if (t.status === 'queued') {
    if (st && st.state !== 'online') return h('div', { class: 'chip warn', dataset: { chip: m.id } }, `Agent ${agentStateText(st)}，消息已保存，上线后会处理`);
    return h('div', { class: 'chip', dataset: { chip: m.id } }, h('span', { class: 'spin' }), '等待 Agent…');
  }
  if (t.status === 'leased') return h('div', { class: 'chip', dataset: { chip: m.id } }, h('span', { class: 'spin' }), 'Agent 处理中…');
  if (t.status === 'expired' || t.status === 'failed') {
    return h('div', { class: 'chip warn', dataset: { chip: m.id } }, t.status === 'expired' ? '未及时处理' : '处理失败', ' ', h('button', { class: 'link', type: 'button', onclick: () => retryTask(t) }, '重试'));
  }
  return null;
}

function refreshChip(m) {
  const row = els.msgs.querySelector(`[data-mid="${m.id}"]`);
  if (!row) return;
  const wrap = row.querySelector('.bwrap');
  const old = wrap.querySelector('.chip');
  const nu = taskChip(m);
  if (old && nu) old.replaceWith(nu);
  else if (old) old.remove();
  else if (nu) wrap.append(nu);
  nearBottomStick();
}

function onTask(t) {
  if (!t.trigger_message_id) return;
  const cur = S.tasks.get(t.trigger_message_id);
  if (cur && cur.id > t.id) return;
  S.tasks.set(t.trigger_message_id, t);
  if (t.room_id === S.cur) {
    const m = visibleList().find((x) => x.id === t.trigger_message_id);
    m && refreshChip(m);
  }
}

async function retryTask(t) {
  try {
    const { task } = await post(`/api/tasks/${t.id}/retry`);
    onTask(task);
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ------------------------------------------------------------------ 实时事件
function onNewMessage(m) {
  if (m.room_id === S.cur && m.sender_id !== S.me.id) hideTyping();
  if (m.room_id !== S.cur) return;
  const st = S.msgs.get(S.cur);
  if (!st) return;
  // 替换同一条的本地待发送项（按 client_msg_id）
  const i = st.list.findIndex((x) => (x.local && x.client_msg_id && x.client_msg_id === m.client_msg_id && m.sender_id === S.me.id) || (!x.local && x.id === m.id));
  if (i >= 0) {
    if (!st.list[i].local) return;
    st.list[i] = m;
    replaceRow(`p-${m.client_msg_id}`, m);
    return;
  }
  if (st.hasAfter) {
    newCount += 1;
    updateToBottom();
    return;
  }
  const stick = nearBottom() || m.sender_id === S.me.id;
  const pend = st.list.filter((x) => x.local);
  const real = st.list.filter((x) => !x.local);
  st.list = [...real, m, ...pend];
  const prev = real[real.length - 1] || null;
  const rows = rowEls(m, prev);
  const firstPending = els.msgs.querySelector('.row.pending');
  rows.forEach((el) => (firstPending ? firstPending.before(el) : els.msgs.append(el)));
  if (stick) scrollToBottom(true);
  else {
    newCount += 1;
    updateToBottom();
  }
  markReadSoon();
}

function replaceRow(oldMid, m) {
  const old = els.msgs.querySelector(`[data-mid="${oldMid}"]`);
  const st = S.msgs.get(S.cur);
  const idx = st.list.findIndex((x) => x.id === m.id);
  const prev = idx > 0 ? st.list[idx - 1] : null;
  const row = messageRow(m, prev);
  if (old) old.replaceWith(row);
  else els.msgs.append(row);
}

function onUpdateMessage(m) {
  if (m.room_id !== S.cur) return;
  const st = S.msgs.get(S.cur);
  const i = st?.list.findIndex((x) => x.id === m.id);
  if (i === undefined || i < 0) return;
  m.starred = st.list[i].starred && !m.deleted;
  st.list[i] = m;
  replaceRow(m.id, m);
}

async function resync() {
  // 断线重连：按序号补齐当前会话缺失的消息
  const id = S.cur;
  const st = id && S.msgs.get(id);
  if (!st || st.hasAfter) {
    emit('rooms:reload');
    return;
  }
  emit('rooms:reload');
  const real = st.list.filter((m) => !m.local);
  let after = real.length ? real[real.length - 1].seq : 0;
  try {
    for (let n = 0; n < 20; n++) {
      const d = await get(`/api/rooms/${id}/messages${qs({ after_seq: after, limit: 100 })}`);
      for (const m of d.messages) onNewMessage(m);
      if (!d.messages.length || !d.has_more_after) break;
      after = d.messages[d.messages.length - 1].seq;
    }
    loadTasks(id);
  } catch {}
  retryFailed();
}

// 发送失败的消息在网络恢复（online 事件 / WebSocket 重连 / 定时）后自动重试；同一个 client_msg_id，服务器幂等去重，不会重复
function retryFailed() {
  for (const st of S.msgs.values()) for (const m of st.list.filter((x) => x.local && x.failed && !x.retrying)) {
    m.retrying = true;
    m.failed = false;
    if (m.room_id === S.cur) replaceRow(m.id, m);
    postPending(m).finally(() => (m.retrying = false));
  }
}

// ------------------------------------------------------------------ 滚动与分页
const nearBottom = () => els.msgs.scrollHeight - els.msgs.scrollTop - els.msgs.clientHeight < 140;
function nearBottomStick() {
  if (stickNext) els.msgs.scrollTop = els.msgs.scrollHeight;
}
let stickNext = false;

function scrollToBottom(smooth) {
  stickNext = true;
  els.msgs.scrollTo({ top: els.msgs.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
  setTimeout(() => {
    els.msgs.scrollTop = els.msgs.scrollHeight;
    stickNext = false;
    newCount = 0;
    updateToBottom();
  }, smooth ? 260 : 60);
}

function scrollToSeq(seq) {
  const m = visibleList().find((x) => x.seq === seq);
  if (!m) return;
  const el = els.msgs.querySelector(`[data-mid="${m.id}"]`);
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.classList.add('hl');
    setTimeout(() => el.classList.remove('hl'), 2200);
  }
}

function updateToBottom() {
  const st = S.msgs.get(S.cur);
  const show = st && (st.hasAfter || !nearBottom());
  els.toBottom.hidden = !show;
  if (!show) return;
  els.toBottom.replaceChildren(icon('down', 16), st.hasAfter ? ' 回到最新消息' : newCount ? ` ${newCount} 条新消息` : ' 回到底部');
}

async function onScroll() {
  const st = S.msgs.get(S.cur);
  if (!st) return;
  stickNext = false;
  if (els.msgs.scrollTop < 120 && st.hasBefore && !loadingOlder) await loadOlder();
  if (nearBottom() && st.hasAfter && !loadingNewer) await loadNewer();
  if (nearBottom() && !st.hasAfter) {
    newCount = 0;
    markReadSoon();
  }
  updateToBottom();
}

async function loadOlder() {
  const st = S.msgs.get(S.cur);
  const first = st.list.find((m) => !m.local);
  if (!first) return;
  loadingOlder = true;
  try {
    const d = await get(`/api/rooms/${S.cur}/messages${qs({ before_seq: first.seq, limit: 50 })}`);
    const prevH = els.msgs.scrollHeight;
    const prevTop = els.msgs.scrollTop;
    st.list = [...d.messages, ...st.list];
    st.hasBefore = d.has_more_before;
    renderAll();
    els.msgs.scrollTop = els.msgs.scrollHeight - prevH + prevTop;
  } catch {}
  loadingOlder = false;
}

async function loadNewer() {
  const st = S.msgs.get(S.cur);
  const real = st.list.filter((m) => !m.local);
  const last = real[real.length - 1];
  if (!last) return;
  loadingNewer = true;
  try {
    const d = await get(`/api/rooms/${S.cur}/messages${qs({ after_seq: last.seq, limit: 50 })}`);
    st.list = [...real, ...d.messages, ...st.list.filter((m) => m.local)];
    st.hasAfter = d.has_more_after;
    renderAll();
  } catch {}
  loadingNewer = false;
}

const markReadSoon = debounce(async () => {
  const id = S.cur;
  const st = id && S.msgs.get(id);
  if (!st || st.hasAfter || document.visibilityState !== 'visible' || !nearBottom()) return;
  const last = [...st.list].reverse().find((m) => !m.local);
  const r = room(id);
  if (!last || !r) return;
  if (last.seq <= r.last_read_seq && r.unread === 0) return;
  try {
    const res = await post(`/api/rooms/${id}/read`, { seq: last.seq });
    r.last_read_seq = res.last_read_seq;
    r.unread = 0;
    emit('rooms:render');
  } catch {}
}, 250);

// ------------------------------------------------------------------ 跳转与搜索
export async function jumpTo(d) {
  let { room_id, seq, id } = d;
  if (!room_id || !seq) {
    try {
      const loc = await get(`/api/messages/${id}/locate`);
      room_id = loc.room_id;
      seq = loc.seq;
    } catch (e) {
      toast('找不到这条原文（可能已被删除）', 'err');
      return;
    }
  }
  els.searchPane.hidden = true;
  if (room_id !== S.cur) {
    S.view = 'chat';
    await openRoom(room_id, { jumpSeq: seq });
    return;
  }
  const m = visibleList().find((x) => x.seq === seq);
  if (m && els.msgs.querySelector(`[data-mid="${m.id}"]`)) scrollToSeq(seq);
  else await loadInitial(S.cur, { jumpSeq: seq });
  S.view = 'chat';
  emit('view');
}

function toggleSearch() {
  if (!els.searchPane.hidden) {
    els.searchPane.hidden = true;
    return;
  }
  const input = h('input', { type: 'search', placeholder: '搜索本会话的消息…', 'aria-label': '搜索本会话', class: 'inroom-q' });
  const results = h('div', { class: 'results' }, h('div', { class: 'empty' }, '输入关键词后回车'));
  const run = async () => {
    const q = input.value.trim();
    if (!q) return;
    results.replaceChildren(h('div', { class: 'empty' }, '搜索中…'));
    try {
      const d = await get(`/api/rooms/${S.cur}/messages${qs({ q, limit: 50 })}`);
      results.replaceChildren(...(d.messages.length ? d.messages.map(resultItem) : [h('div', { class: 'empty' }, '没有找到相关消息')]));
    } catch (e) {
      results.replaceChildren(h('div', { class: 'empty' }, e.message));
    }
  };
  input.addEventListener('keydown', (e) => e.key === 'Enter' && !e.isComposing && run());
  els.searchPane.replaceChildren(h('div', { class: 'sp-head' }, input, iconBtn('close', '关闭搜索', () => (els.searchPane.hidden = true))), results);
  els.searchPane.hidden = false;
  input.focus();
}

export function resultItem(m) {
  const who = userById(m.sender_id);
  return h(
    'button',
    { class: 'result', type: 'button', onclick: () => emit('jump', { room_id: m.room_id, seq: m.seq, id: m.id }) },
    h('div', { class: 'r-top' }, h('b', null, who.display_name), h('time', null, `${fmtDay(m.created_at)} ${fmtTime(m.created_at)}`)),
    h('div', { class: 'r-body' }, m.body || '[附件]')
  );
}

// ------------------------------------------------------------------ 消息菜单 / 手势
let menuEl = null;
let menuOpenedAt = 0;
function closeMenu() {
  menuEl && menuEl.remove();
  menuEl = null;
  document.querySelectorAll('.row.picked').forEach((r) => r.classList.remove('picked'));
}
document.addEventListener('pointerdown', (e) => menuEl && Date.now() - menuOpenedAt > 350 && !menuEl.contains(e.target) && closeMenu());
document.addEventListener('keydown', (e) => e.key === 'Escape' && closeMenu());
window.addEventListener('scroll', (e) => menuEl && !menuEl.contains(e.target) && closeMenu(), true);

// 触屏：长按弹出操作条、右滑引用；桌面：右键弹出操作条
function attachGestures(bubble, row, m) {
  let sx = 0, sy = 0, dx = 0, mode = '', timer = 0, quoted = false;
  const clear = () => clearTimeout(timer);
  const reset = () => {
    clear();
    bubble.style.transform = '';
    bubble.classList.remove('swiping');
    row.classList.remove('swipe-ready');
    mode = '';
  };
  bubble.addEventListener(
    'touchstart',
    (e) => {
      if (e.touches.length !== 1) return reset();
      const t = e.touches[0];
      sx = t.clientX;
      sy = t.clientY;
      dx = 0;
      mode = '';
      quoted = false;
      clear();
      timer = setTimeout(() => {
        mode = 'long';
        navigator.vibrate && navigator.vibrate(12);
        openMenu(bubble, m);
      }, 420);
    },
    { passive: true }
  );
  bubble.addEventListener(
    'touchmove',
    (e) => {
      const t = e.touches[0];
      const mx = t.clientX - sx;
      const my = t.clientY - sy;
      if (mode === '' && (Math.abs(mx) > 8 || Math.abs(my) > 8)) {
        clear();
        mode = Math.abs(mx) > Math.abs(my) * 1.4 && mx > 0 ? 'swipe' : 'scroll';
      }
      if (mode === 'swipe') {
        dx = Math.min(mx, 96);
        bubble.classList.add('swiping');
        bubble.style.transform = `translateX(${dx * 0.8}px)`;
        const ready = dx > 64;
        row.classList.toggle('swipe-ready', ready);
        if (ready && !quoted) {
          quoted = true;
          navigator.vibrate && navigator.vibrate(8);
        } else if (!ready) quoted = false;
      }
    },
    { passive: true }
  );
  const end = () => {
    const fire = mode === 'swipe' && dx > 64;
    reset();
    if (fire) {
      setReply(m);
      els.input.focus();
    }
  };
  bubble.addEventListener('touchend', end, { passive: true });
  bubble.addEventListener('touchcancel', reset, { passive: true });
  bubble.addEventListener('contextmenu', (e) => {
    if (e.target.closest('a,img,textarea')) return;
    e.preventDefault();
    openMenu(bubble, m, coarse ? null : { x: e.clientX, y: e.clientY });
  });
}

function moreReactions(m) {
  if (!menuEl) return;
  const panel = buildEmojiPanel((e) => (closeMenu(), react(m, e)), { compact: true });
  menuEl.querySelector('.acts').replaceWith(panel);
  menuEl.querySelector('.quick').remove();
  menuEl.classList.add('wide');
  const b = menuEl.getBoundingClientRect();
  menuEl.style.left = Math.max(8, Math.min(b.left, window.innerWidth - b.width - 8)) + 'px';
  menuEl.style.top = Math.max(8, Math.min(b.top, window.innerHeight - b.height - 8)) + 'px';
}

function openMenu(anchor, m, pt) {
  closeMenu();
  const mine = m.sender_id === S.me.id && m.kind === 'human';
  const items = [
    { label: '引用', icon: 'reply', fn: () => (setReply(m), els.input.focus()) },
    { label: '复制', icon: 'copy', fn: () => copyText(m.body || m.attachment?.transcript || ''), skip: !m.body && !m.attachment?.transcript },
    { label: '转文字', icon: 'mic', fn: () => transcribe(m), skip: !(m.attachment?.is_voice && !m.attachment.transcript && room(m.room_id)?.agent_in_room) },
    { label: m.starred ? '取消收藏' : '收藏', icon: 'star', fn: () => toggleStar(m) },
    { label: '编辑', icon: 'edit', fn: () => startEdit(m), skip: !mine || !m.body },
    { label: '删除', icon: 'trash', fn: () => deleteMessage(m), danger: true, skip: !mine },
  ].filter((i) => !i.skip);
  const quick = h(
    'div',
    { class: 'quick' },
    QUICK.map((e) => h('button', { type: 'button', class: 'qe', 'aria-label': '回应 ' + e, onpointerdown: (ev) => ev.preventDefault(), onclick: () => (closeMenu(), react(m, e)) }, e)),
    h('button', { type: 'button', class: 'qe more-e', 'aria-label': '更多表情', onpointerdown: (ev) => ev.preventDefault(), onclick: () => moreReactions(m) }, icon('plus', 18))
  );
  menuEl = h(
    'div',
    { class: 'popbar', role: 'menu' },
    h('div', { class: 'when' }, fmtFull(m.created_at) + (m.edited_at ? ' · 已编辑' : '')),
    quick,
    h('div', { class: 'acts' },
    items.map((i) =>
      h(
        'button',
        {
          type: 'button',
          role: 'menuitem',
          class: i.danger ? 'danger' : '',
          onpointerdown: (e) => e.preventDefault(),
          onclick: () => {
            closeMenu();
            i.fn();
          },
        },
        icon(i.icon, 20),
        h('span', null, i.label)
      )
    ))
  );
  menuOpenedAt = Date.now();
  anchor.closest('.row')?.classList.add('picked');
  document.body.append(menuEl);
  const b = anchor.getBoundingClientRect();
  const mw = menuEl.offsetWidth;
  const mh = menuEl.offsetHeight;
  const cx = pt ? pt.x : b.left + b.width / 2;
  let left = Math.min(Math.max(8, cx - mw / 2), window.innerWidth - mw - 8);
  let top = pt ? pt.y - mh - 8 : b.top - mh - 8;
  if (top < 8) top = pt ? pt.y + 8 : Math.min(b.bottom + 8, window.innerHeight - mh - 8);
  menuEl.style.left = left + 'px';
  menuEl.style.top = Math.max(8, top) + 'px';
}

async function toggleStar(m) {
  try {
    await post(`/api/messages/${m.id}/star`, { value: !m.starred });
    m.starred = !m.starred;
    replaceRow(m.id, m);
    emit('stars:changed');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function deleteMessage(m) {
  const ok = await ask('删除后，这条消息的正文和附件会被永久清除，依赖它的回顾和记忆也会被清理。', { ok: '永久删除', danger: true, title: '删除消息' });
  if (!ok) return;
  try {
    await del(`/api/messages/${m.id}`);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function startEdit(m) {
  const row = els.msgs.querySelector(`[data-mid="${m.id}"]`);
  const body = row?.querySelector('.body');
  if (!body) return;
  const ta = h('textarea', { class: 'edit-box', rows: '2', 'aria-label': '编辑消息' });
  ta.value = m.body;
  const save = async () => {
    const v = ta.value.trim();
    if (!v || v === m.body) return cancel();
    try {
      const { message } = await patch(`/api/messages/${m.id}`, { body: v });
      onUpdateMessage(message);
    } catch (e) {
      toast(e.message, 'err');
    }
  };
  const cancel = () => replaceRow(m.id, m);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') cancel();
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      save();
    }
  });
  body.replaceChildren(ta, h('div', { class: 'edit-actions' }, h('button', { class: 'btn', type: 'button', onclick: cancel }, '取消'), h('button', { class: 'btn primary', type: 'button', onclick: save }, '保存')));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

// ------------------------------------------------------------------ 表情面板
function toggleEmoji() {
  if (!els.emojiHolder.hidden) return hideEmoji();
  if (!els.emojiPanel) {
    els.emojiPanel = buildEmojiPanel(insertEmoji);
    els.emojiHolder.append(els.emojiPanel);
  }
  els.emojiHolder.hidden = false;
  if (coarse) els.input.blur();
  nearBottomStick();
}

function hideEmoji() {
  if (els.emojiHolder) els.emojiHolder.hidden = true;
}

function insertEmoji(e) {
  const t = els.input;
  const s = t.selectionStart ?? t.value.length;
  const en = t.selectionEnd ?? s;
  t.value = t.value.slice(0, s) + e + t.value.slice(en);
  const pos = s + e.length;
  t.setSelectionRange(pos, pos);
  autosize();
  saveDraft();
  if (!coarse) t.focus();
}

// ------------------------------------------------------------------ 正在输入
let lastTypingSent = 0;
function sendTyping() {
  const r = room(S.cur);
  if (!r || r.type === 'agent_dm' || !els.input.value.trim()) return;
  const now = Date.now();
  if (now - lastTypingSent < 3000) return;
  lastTypingSent = now;
  wsSend({ type: 'typing', room_id: S.cur });
}

let typingTimer = 0;
function hideTyping() {
  clearTimeout(typingTimer);
  els.typing.hidden = true;
}
function onTyping(d) {
  if (d.room_id !== S.cur || d.user_id === S.me.id) return;
  els.typing.replaceChildren(h('span', { class: 'tdots' }, h('i'), h('i'), h('i')), ' ' + userById(d.user_id).display_name + ' 正在输入');
  els.typing.hidden = false;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(hideTyping, 5000);
}

// ------------------------------------------------------------------ 语音
let rec = null;
let recUi = null;
async function startVoice() {
  if (rec || !S.cur) return;
  const ctrl = await startRecording((ms) => recUi && recUi.setTime(ms));
  if (!ctrl) return;
  rec = ctrl;
  hideEmoji();
  recUi = recordingBar({ onCancel: cancelVoice, onSend: () => finishVoice() });
  els.rowMain.hidden = true;
  els.rowMain.after(recUi.el);
  ctrl.onLimit = () => {
    toast('已达到最长录音时间，自动发送');
    finishVoice();
  };
}

function closeRecUi() {
  recUi && recUi.el.remove();
  recUi = null;
  rec = null;
  els.rowMain.hidden = false;
}

function cancelVoice() {
  rec && rec.cancel();
  closeRecUi();
}

async function finishVoice() {
  if (!rec) return;
  const ctrl = rec;
  const res = await ctrl.stop();
  closeRecUi();
  if (!res) return;
  if (res.tooShort) return toast('说话时间太短');
  const roomId = S.cur;
  uploading = true;
  renderAttBar();
  try {
    const fd = new FormData();
    fd.append('file', res.blob, `voice-${Date.now()}.${res.ext}`);
    fd.append('duration_ms', String(res.duration_ms));
    const { attachment } = await api('POST', `/api/rooms/${roomId}/attachments`, fd);
    pendingAtt = attachment;
  } catch (e) {
    toast(e.message, 'err');
  }
  uploading = false;
  renderAttBar();
  if (pendingAtt && S.cur === roomId) await send();
}

// ------------------------------------------------------------------ 表情回应
async function transcribe(m) {
  try {
    await api('POST', `/api/messages/${m.id}/transcribe`);
    toast('正在转文字…完成后会显示在语音下面');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function react(m, emoji) {
  const mine = (m.reactions || []).find((r) => r.emoji === emoji)?.user_ids.includes(S.me.id);
  try {
    const d = await api('POST', `/api/messages/${m.id}/reactions`, { emoji, on: !mine });
    applyReactions(m.room_id, m.id, d.reactions);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function applyReactions(roomId, mid, reactions) {
  const st = S.msgs.get(roomId);
  const m = st?.list.find((x) => x.id === mid);
  if (!m) return;
  m.reactions = reactions;
  if (roomId === S.cur) replaceRow(m.id, m);
}

function onReaction(d) {
  applyReactions(d.room_id, d.message_id, d.reactions);
}

function reactionsEl(m) {
  if (!m.reactions || !m.reactions.length) return null;
  return h(
    'div',
    { class: 'reacts' },
    m.reactions.map((r) => {
      const mine = r.user_ids.includes(S.me.id);
      return h('button', { type: 'button', class: 'react' + (mine ? ' mine' : ''), title: r.user_ids.map((i) => userById(i).display_name).join('、'), onclick: () => react(m, r.emoji) }, r.emoji, r.user_ids.length > 1 ? h('small', null, String(r.user_ids.length)) : null);
    })
  );
}

// ------------------------------------------------------------------ 输入框
let sendMode = '';
function updateSendState() {
  const has = !!els.input.value.trim() || !!pendingAtt;
  const mode = has ? 'send' : voiceSupported() ? 'mic' : 'idle';
  els.send.disabled = mode === 'idle';
  if (mode !== sendMode) {
    sendMode = mode;
    els.send.replaceChildren(icon(mode === 'mic' ? 'mic' : 'send', 20));
    els.send.classList.toggle('mic', mode === 'mic');
    const label = mode === 'mic' ? '录制语音' : '发送';
    els.send.title = label;
    els.send.setAttribute('aria-label', label);
  }
}

function onSendClick() {
  if (sendMode === 'mic') return startVoice();
  send();
}

function autosize() {
  updateSendState();
  els.input.style.height = 'auto';
  const sh = els.input.scrollHeight; // 窄屏下视图尚未显示时为 0，此时保持自动高度
  els.input.style.height = sh ? Math.min(sh, 160) + 'px' : '';
}

function saveDraft() {
  if (S.cur) drafts.set(S.cur, { text: els.input.value, reply: replyTo });
}

function restoreDraft() {
  const d = drafts.get(S.cur) || { text: '', reply: null };
  els.input.value = d.text;
  replyTo = d.reply;
  renderReplyBar();
  renderAttBar();
  autosize();
  els.mention.hidden = true;
}

function setReply(m) {
  replyTo = m;
  renderReplyBar();
  els.input.focus();
}

function renderReplyBar() {
  if (!replyTo) {
    els.replybar.hidden = true;
    return;
  }
  els.replybar.hidden = false;
  els.replybar.replaceChildren(
    icon('reply', 14),
    h('div', { class: 'rb-text' }, h('b', null, userById(replyTo.sender_id).display_name), h('span', null, replyTo.body || '[附件]')),
    iconBtn('close', '取消回复', () => ((replyTo = null), renderReplyBar(), saveDraft()))
  );
}

function renderAttBar() {
  updateSendState();
  if (!pendingAtt && !uploading) {
    els.attbar.hidden = true;
    return;
  }
  els.attbar.hidden = false;
  els.attbar.replaceChildren(
    icon('clip', 14),
    h('span', { class: 'fname' }, uploading ? '上传中…' : pendingAtt.filename),
    pendingAtt ? h('small', null, fmtSize(pendingAtt.size)) : null,
    iconBtn('close', '移除附件', () => ((pendingAtt = null), renderAttBar()))
  );
}

// 手机拍的照片常有 5–12 MB：上传前缩到长边 2048 以内、重新编码为 JPEG，省流量也更快。GIF、小图和失败时保持原样
async function shrinkImage(file) {
  try {
    if (!file.type.startsWith('image/') || file.type === 'image/gif' || file.size < 1_200_000 || !window.createImageBitmap) return file;
    const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
    const k = Math.min(1, 2048 / Math.max(bmp.width, bmp.height));
    const w = Math.round(bmp.width * k);
    const hh = Math.round(bmp.height * k);
    const cv = document.createElement('canvas');
    cv.width = w;
    cv.height = hh;
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, w, hh);
    ctx.drawImage(bmp, 0, 0, w, hh);
    bmp.close && bmp.close();
    const blob = await new Promise((r) => cv.toBlob(r, 'image/jpeg', 0.85));
    if (!blob || blob.size >= file.size) return file;
    return new File([blob], (file.name || 'photo').replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' });
  } catch {
    return file;
  }
}

async function uploadFile(file) {
  if (!S.cur) return;
  uploading = true;
  renderAttBar();
  try {
    const fd = new FormData();
    const f2 = await shrinkImage(file);
    fd.append('file', f2, f2.name || 'pasted-image.png');
    const { attachment } = await api('POST', `/api/rooms/${S.cur}/attachments`, fd);
    pendingAtt = attachment;
  } catch (e) {
    toast(e.message, 'err');
  }
  uploading = false;
  els.file.value = '';
  renderAttBar();
  els.input.focus();
}

let mentionIdx = 0;
let mentionList = [];
function checkMention() {
  const r = room(S.cur);
  if (!r) return;
  const pos = els.input.selectionStart;
  const m = els.input.value.slice(0, pos).match(/(?:^|\s)@([A-Za-z0-9_\-]*)$/);
  if (!m) {
    els.mention.hidden = true;
    return;
  }
  const q = m[1].toLowerCase();
  mentionList = r.members.filter((u) => u.id !== S.me.id && (u.username.toLowerCase().startsWith(q) || u.display_name.toLowerCase().includes(q)));
  if (!mentionList.length) {
    els.mention.hidden = true;
    return;
  }
  mentionIdx = 0;
  renderMention();
}

function renderMention() {
  els.mention.hidden = false;
  els.mention.replaceChildren(
    ...mentionList.map((u, i) =>
      h(
        'button',
        { type: 'button', role: 'option', class: i === mentionIdx ? 'sel' : '', 'aria-selected': i === mentionIdx ? 'true' : 'false', onmousedown: (e) => (e.preventDefault(), pickMention(u)) },
        avatar(u, 24),
        h('span', null, u.display_name),
        h('small', null, '@' + u.username),
        u.role === 'agent' ? h('span', { class: 'agent-badge' }, 'Agent') : null
      )
    )
  );
}

function pickMention(u) {
  const pos = els.input.selectionStart;
  const before = els.input.value.slice(0, pos).replace(/@[A-Za-z0-9_\-]*$/, '@' + u.username + ' ');
  els.input.value = before + els.input.value.slice(pos);
  els.input.setSelectionRange(before.length, before.length);
  els.mention.hidden = true;
  autosize();
  saveDraft();
  els.input.focus();
}

function insertMention(username) {
  const v = els.input.value;
  const sep = v && !/\s$/.test(v) ? ' ' : '';
  els.input.value = v + sep + '@' + username + ' ';
  els.input.focus();
  autosize();
  saveDraft();
}

function onKeydown(e) {
  if (!els.mention.hidden) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      mentionIdx = (mentionIdx + (e.key === 'ArrowDown' ? 1 : -1) + mentionList.length) % mentionList.length;
      renderMention();
      return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      pickMention(mentionList[mentionIdx]);
      return;
    }
    if (e.key === 'Escape') {
      els.mention.hidden = true;
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229 && !coarse) {
    e.preventDefault();
    send();
  }
}

// ------------------------------------------------------------------ 发送（乐观显示 + 幂等重试）
async function send() {
  if (uploading) return toast('附件还在上传…');
  const body = els.input.value.trim();
  if (!body && !pendingAtt) return;
  if (!S.cur) return;
  let st = S.msgs.get(S.cur);
  if (st && st.hasAfter) {
    await goLatest();
    st = S.msgs.get(S.cur);
  }
  const cid = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now()) + Math.random();
  const m = {
    id: 'p-' + cid,
    local: true,
    client_msg_id: cid,
    room_id: S.cur,
    sender_id: S.me.id,
    kind: 'human',
    body,
    reply_to: replyTo ? replyTo.id : null,
    reply_preview: replyTo ? { id: replyTo.id, seq: replyTo.seq, sender_id: replyTo.sender_id, body: replyTo.body, deleted: false, has_attachment: !!replyTo.attachment } : null,
    attachment: pendingAtt,
    created_at: new Date().toISOString(),
    version: 1,
  };
  st.list.push(m);
  const prev = st.list[st.list.length - 2] || null;
  els.msgs.append(...rowEls(m, prev));
  els.input.value = '';
  replyTo = null;
  pendingAtt = null;
  renderReplyBar();
  renderAttBar();
  autosize();
  drafts.delete(S.cur);
  scrollToBottom(false);
  await postPending(m);
  els.input.focus();
}

async function postPending(m) {
  try {
    const { message } = await post(`/api/rooms/${m.room_id}/messages`, { body: m.body, client_msg_id: m.client_msg_id, reply_to: m.reply_to, attachment_id: m.attachment ? m.attachment.id : null });
    if (m.room_id !== S.cur) return;
    const st = S.msgs.get(S.cur);
    const i = st.list.findIndex((x) => x.local && x.client_msg_id === m.client_msg_id);
    if (i >= 0) {
      st.list[i] = message;
      replaceRow(m.id, message);
    } else {
      // WebSocket 已先一步带来了这条消息
      els.msgs.querySelector(`[data-mid="${m.id}"]`)?.remove();
    }
  } catch (e) {
    m.failed = true;
    if (m.room_id === S.cur && els.msgs.querySelector(`[data-mid="${m.id}"]`)) replaceRow(m.id, m);
    if (e.status && e.status !== 0 && !m.toasted) {
      m.toasted = true;
      toast('发送失败：' + e.message, 'err');
    }
  }
}

function retrySend(m) {
  m.failed = false;
  replaceRow(m.id, m);
  postPending(m);
}

function discardPending(m) {
  const st = S.msgs.get(S.cur);
  st.list = st.list.filter((x) => x !== m);
  els.msgs.querySelector(`[data-mid="${m.id}"]`)?.remove();
}
