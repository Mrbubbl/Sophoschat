// 通用小工具：DOM 构建、安全的 Markdown 渲染（不使用 innerHTML）、时间格式、图标、提示与对话框
import { openLayer } from './nav.js';

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'value' && tag === 'textarea') el.value = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ---------------------------------------------------------------- 图标（内联 SVG，来自固定字符串）
const ICONS = {
  back: '<path d="M15 18l-6-6 6-6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  send: '<path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/>',
  clip: '<path d="M21 12.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l9-9a3.7 3.7 0 0 1 5.2 5.2l-9 9a1.8 1.8 0 0 1-2.6-2.6l8.4-8.4"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 8V4M9 14h.01M15 14h.01M2 14h2M20 14h2"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  star: '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
  reply: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/>',
  close: '<path d="M18 6L6 18M6 6l12 12"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  download: '<path d="M12 3v12m0 0l-4-4m4 4l4-4M4 20h16"/>',
  archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2m-9 0l1 14h8l1-14"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  at: '<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/>',
  mic: '<rect x="9" y="3" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
  smile: '<circle cx="12" cy="12" r="9"/><path d="M8 14a4.5 4.5 0 0 0 8 0M9 9.5h.01M15 9.5h.01"/>',
  play: '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
  pause: '<path d="M8 4v16M16 4v16"/>',
  device: '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
  phone: '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
  'call-end': '<path d="M10.7 13.3a16 16 0 0 0 3 2.3l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9M2 2l20 20"/>',
  video: '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3"/>',
  'video-off': '<path d="M2 2l20 20M16 16v1a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2m5 0h3a2 2 0 0 1 2 2v3l6-3v10"/>',
  'mic-off': '<path d="M2 2l20 20M9 9v2a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.9-.7M5 11a7 7 0 0 0 11.7 5.2M19 11a7 7 0 0 1-.6 2.8M12 18v3"/>',
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  flip: '<path d="M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5"/>',
  pip: '<rect x="2" y="4" width="20" height="16" rx="2"/><rect x="12" y="12" width="8" height="6" rx="1" fill="currentColor"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
  pin: '<path d="M12 17v5M9 3h6l-1 7 4 3v2H6v-2l4-3z"/>',
  unread: '<circle cx="12" cy="12" r="4" fill="currentColor"/><circle cx="12" cy="12" r="9"/>',
  speaker: '<path d="M11 5L6 9H3v6h3l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.6-.9 1.2-1.8-.4-1-.1-2.2 1.2-2.2H17a4 4 0 0 0 4-4c0-5.5-4-10-9-10z"/><circle cx="7.5" cy="11" r="1.2"/><circle cx="10" cy="7" r="1.2"/><circle cx="15" cy="7.5" r="1.2"/>',
  qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v.01M14 20h.01M17 17h4v4h-4"/>',
  share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
  type: '<path d="M4 7V4h16v3M9 20h6M12 4v16"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
};
export function icon(name, size = 18) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('width', size);
  s.setAttribute('height', size);
  s.setAttribute('fill', 'none');
  s.setAttribute('stroke', 'currentColor');
  s.setAttribute('stroke-width', '2');
  s.setAttribute('stroke-linecap', 'round');
  s.setAttribute('stroke-linejoin', 'round');
  s.setAttribute('aria-hidden', 'true');
  s.innerHTML = ICONS[name] || '';
  return s;
}
export function iconBtn(name, label, onclick, cls = '') {
  return h('button', { class: 'icon-btn ' + cls, type: 'button', title: label, 'aria-label': label, onclick }, icon(name));
}

// ---------------------------------------------------------------- 时间
const pad = (n) => String(n).padStart(2, '0');
export function fmtTime(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function dayKey(iso) {
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export function fmtDay(iso) {
  const d = new Date(iso);
  const t = new Date();
  const y = new Date(Date.now() - 86400000);
  if (dayKey(iso) === dayKey(t.toISOString())) return '今天';
  if (dayKey(iso) === dayKey(y.toISOString())) return '昨天';
  return d.getFullYear() === t.getFullYear() ? `${d.getMonth() + 1}月${d.getDate()}日` : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}
export function fmtRel(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if (dayKey(iso) === dayKey(now.toISOString())) return fmtTime(iso);
  const diff = (now - d) / 86400000;
  if (diff < 2) return '昨天';
  if (diff < 7) return '周' + '日一二三四五六'[d.getDay()];
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
export function fmtFull(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
export function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(1) + ' MB';
}
export const debounce = (fn, ms) => {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
};

// ---------------------------------------------------------------- 头像
const COLORS = ['#0f766e', '#2563eb', '#b45309', '#9333ea', '#be123c', '#4d7c0f', '#0e7490', '#c2410c'];
export function avatar(user, size = 36) {
  const isAgent = user && user.role === 'agent';
  const name = (user && user.display_name) || '?';
  const el = h('div', { class: 'avatar' + (isAgent ? ' agent' : ''), style: { width: size + 'px', height: size + 'px', fontSize: Math.round(size * 0.42) + 'px' }, 'aria-hidden': 'true' });
  if (user && user.username) {
    el.dataset.uid = user.id; // 头像更换后，页面上同一个人的头像一起刷新
    el.dataset.size = size;
  }
  if (user && user.avatar_url) {
    el.classList.add('has-img');
    el.append(h('img', { src: user.avatar_url, alt: '', draggable: 'false' }));
    return el;
  }
  if (isAgent) {
    el.append(icon('bot', Math.round(size * 0.55)));
  } else {
    el.style.background = COLORS[(user ? user.id : 0) % COLORS.length];
    el.textContent = [...name][0].toUpperCase();
  }
  return el;
}

// ---------------------------------------------------------------- 安全的 Markdown：只构造 DOM 节点，不解析 HTML
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(~~[^~\n]+~~)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s<>"'）)]+)|((?<![\w@])@[A-Za-z0-9_\-]{2,32})|((?<![\w*])\*[^*\s][^*\n]*\*(?![\w*]))/g;

export function inline(text, ctx = {}) {
  const out = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const [tok] = m;
    if (m[1]) out.push(h('code', null, tok.slice(1, -1)));
    else if (m[2]) out.push(h('strong', null, inline(tok.slice(2, -2), ctx)));
    else if (m[3]) out.push(h('del', null, inline(tok.slice(2, -2), ctx)));
    else if (m[4]) {
      const mm = tok.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
      out.push(h('a', { href: mm[2], target: '_blank', rel: 'noopener noreferrer nofollow' }, mm[1]));
    } else if (m[5]) out.push(h('a', { href: tok, target: '_blank', rel: 'noopener noreferrer nofollow' }, tok));
    else if (m[6]) {
      const name = tok.slice(1).toLowerCase();
      const hit = (ctx.members || []).find((u) => u.username.toLowerCase() === name);
      out.push(hit ? h('span', { class: 'mention' + (hit.role === 'agent' ? ' agent' : '') }, tok) : tok);
    } else if (m[7]) out.push(h('em', null, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// 块级解析：代码块（带语言和复制）、表格（含对齐）、标题、分隔线、多行/嵌套引用、有序/无序/任务列表（可嵌套）、段落
const FENCE = /^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$/;
const LIST_ITEM = /^(\s*)([-*+•]|\d{1,9}[.)])\s+(.*)$/;
const HR = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const LANG_NAMES = { js: 'JavaScript', javascript: 'JavaScript', ts: 'TypeScript', typescript: 'TypeScript', py: 'Python', python: 'Python', sh: 'Shell', bash: 'Shell', shell: 'Shell', zsh: 'Shell', json: 'JSON', html: 'HTML', css: 'CSS', sql: 'SQL', go: 'Go', rust: 'Rust', java: 'Java', c: 'C', cpp: 'C++', 'c++': 'C++', yaml: 'YAML', yml: 'YAML', md: 'Markdown', markdown: 'Markdown', diff: 'Diff', text: '文本', txt: '文本' };

function codeBlock(code, lang) {
  const label = LANG_NAMES[(lang || '').toLowerCase()] || lang || '代码';
  const btn = h('button', { type: 'button', class: 'cb-copy', 'aria-label': '复制代码' }, '复制');
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await copyText(code);
    btn.textContent = '已复制';
    setTimeout(() => (btn.textContent = '复制'), 1400);
  });
  return h('div', { class: 'codeblock' }, h('div', { class: 'cb-head' }, h('span', null, label), btn), h('pre', null, h('code', null, code)));
}

function splitCells(line) {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
  return t.split(/(?<!\\)\|/).map((x) => x.trim().replace(/\\\|/g, '|'));
}

function tableBlock(lines, i, ctx) {
  const headers = splitCells(lines[i]).slice(0, 20);
  const aligns = splitCells(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : ''));
  const cell = (tag, txt, k) => h(tag, aligns[k] ? { class: 'al-' + aligns[k] } : null, inline(txt, ctx));
  const tbody = h('tbody');
  let j = i + 2;
  while (j < lines.length && lines[j].includes('|') && lines[j].trim()) {
    const row = splitCells(lines[j]);
    tbody.append(h('tr', null, headers.map((_, k) => cell('td', row[k] || '', k))));
    j++;
  }
  const el = h('div', { class: 'md-table-scroll' }, h('table', null, h('thead', null, h('tr', null, headers.map((t, k) => cell('th', t, k)))), tbody));
  return [el, j];
}

function listItemContent(text, ctx) {
  const task = text.match(/^\[([ xX])\]\s+(.*)$/);
  if (task) {
    const done = task[1] !== ' ';
    return [h('span', { class: 'task' + (done ? ' done' : '') }, h('span', { class: 'tbox', 'aria-hidden': 'true' }, done ? '✓' : ''), h('span', null, inline(task[2], ctx)))];
  }
  return inline(text, ctx);
}

function listBlock(lines, i, ctx) {
  // 以缩进决定层级；连续的非列表、缩进行视为上一项的续行
  const root = { indent: -1, el: null, items: [] };
  const stack = [];
  let j = i;
  let lastLi = null;
  while (j < lines.length) {
    const line = lines[j];
    const m = line.match(LIST_ITEM);
    if (!m) {
      if (lastLi && line.trim() && /^\s{2,}\S/.test(line) && !FENCE.test(line)) {
        lastLi.append(h('br'), ...inline(line.trim(), ctx));
        j++;
        continue;
      }
      break;
    }
    const indent = m[1].replace(/\t/g, '    ').length;
    const ordered = /\d/.test(m[2]);
    while (stack.length && indent < stack[stack.length - 1].indent) stack.pop();
    let top = stack[stack.length - 1];
    if (!top || indent > top.indent + 1) {
      const el = h(ordered ? 'ol' : 'ul');
      if (ordered && parseInt(m[2], 10) !== 1) el.setAttribute('start', String(parseInt(m[2], 10)));
      if (top && lastLi) lastLi.append(el);
      top = { indent, el };
      stack.push(top);
      if (!root.el) root.el = el;
    }
    lastLi = h('li', null, listItemContent(m[3], ctx));
    top.el.append(lastLi);
    j++;
    if (stack.length > 6) break;
  }
  return [root.el, j];
}

export function renderMarkdown(text, ctx = {}, depth = 0) {
  const root = h('div', { class: 'md' });
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  let buf = [];
  const flush = () => {
    if (buf.length) {
      const chunk = buf.join('\n');
      if (chunk.trim()) root.append(h('div', { class: 'p' }, inline(chunk, ctx)));
      buf = [];
    }
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(FENCE);
    if (fence) {
      flush();
      const mark = fence[1];
      const code = [];
      let j = i + 1;
      while (j < lines.length && !(lines[j].trim().startsWith(mark[0].repeat(mark.length)) && /^\s*(`{3,}|~{3,})\s*$/.test(lines[j]))) code.push(lines[j++]);
      root.append(codeBlock(code.join('\n'), fence[2]));
      i = j + 1;
      continue;
    }
    if (line.includes('|') && TABLE_SEP.test(lines[i + 1] || '')) {
      flush();
      const [el, j] = tableBlock(lines, i, ctx);
      root.append(el);
      i = j;
      continue;
    }
    if (HR.test(line) && !buf.length) {
      root.append(h('hr'));
      i++;
      continue;
    }
    const hd = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (hd) {
      flush();
      root.append(h('div', { class: 'p hd h' + Math.min(hd[1].length, 4), role: 'heading', 'aria-level': String(Math.min(hd[1].length + 1, 6)) }, inline(hd[2], ctx)));
      i++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const inner = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) inner.push(lines[i++].replace(/^\s*>\s?/, ''));
      root.append(depth < 3 ? h('blockquote', null, renderMarkdown(inner.join('\n'), ctx, depth + 1)) : h('blockquote', null, inner.join('\n')));
      continue;
    }
    if (LIST_ITEM.test(line)) {
      flush();
      const [el, j] = listBlock(lines, i, ctx);
      if (el) root.append(el);
      i = j;
      continue;
    }
    if (!line.trim()) {
      flush();
      i++;
      continue;
    }
    buf.push(line);
    i++;
  }
  flush();
  return root;
}

// 纯文本（朗读、预览用）：去掉常见 Markdown 记号
export function plainText(md) {
  return String(md || '')
    .replace(/```[\s\S]*?```/g, ' （代码略） ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/https?:\/\/\S+/g, '链接')
    .replace(/^\s*#{1,6}\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-*+•]\s+(\[[ xX]\]\s+)?/gm, '')
    .replace(/\*\*|__|~~|\*|\|/g, ' ')
    .replace(/^\s*:?-{3,}:?.*$/gm, '')
    .replace(/[ \t]+/g, ' ')
    .trim();
}

// ---------------------------------------------------------------- 提示 / 对话框
export function toast(msg, kind = '') {
  let box = $('#toasts');
  if (!box) {
    box = h('div', { id: 'toasts', 'aria-live': 'polite' });
    document.body.append(box);
  }
  const t = h('div', { class: 'toast ' + kind }, msg);
  box.append(t);
  setTimeout(() => t.classList.add('out'), 3200);
  setTimeout(() => t.remove(), 3600);
}

export function modal({ title, body, actions = [], wide = false, onClose }) {
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey, true);
    overlay.remove();
    leave(); // 同步浏览器历史（返回键）
    onClose && onClose();
  };
  const leave = openLayer(close);
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    }
  };
  const card = h(
    'div',
    { class: 'modal' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('div', { class: 'modal-head' }, h('h2', null, title), iconBtn('close', '关闭', close)),
    h('div', { class: 'modal-body' }, body),
    actions.length ? h('div', { class: 'modal-actions' }, actions) : null
  );
  const overlay = h('div', { class: 'overlay', onmousedown: (e) => e.target === overlay && close() }, card);
  document.body.append(overlay);
  document.addEventListener('keydown', onKey, true);
  const first = card.querySelector('input,textarea,select,button.primary');
  first && first.focus();
  return close;
}

export function ask(message, { ok = '确定', danger = false, title = '请确认' } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (!done) {
        done = true;
        resolve(v);
      }
    };
    const close = modal({
      title,
      body: h('p', { class: 'ask' }, message),
      actions: [
        h('button', { class: 'btn', type: 'button', onclick: () => (finish(false), close()) }, '取消'),
        h('button', { class: 'btn primary' + (danger ? ' danger' : ''), type: 'button', onclick: () => (finish(true), close()) }, ok),
      ],
      onClose: () => finish(false),
    });
  });
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制');
  } catch {
    const ta = h('textarea', { style: { position: 'fixed', opacity: 0 } });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try {
      document.execCommand('copy');
      toast('已复制');
    } catch {
      toast('复制失败，请手动选择', 'err');
    }
    ta.remove();
  }
}

export function toggle(label, checked, onChange, hint) {
  const input = h('input', { type: 'checkbox', role: 'switch', checked: checked || null });
  input.checked = !!checked;
  input.addEventListener('change', () => onChange(input.checked, input));
  return h('label', { class: 'switch-row' }, h('span', { class: 'sw-text' }, h('span', null, label), hint ? h('small', null, hint) : null), h('span', { class: 'switch' }, input, h('span', { class: 'slider' })));
}

// 把选中的图片裁成居中的正方形、缩到 size×size 的 JPEG（头像用）
export async function squareJpeg(file, size = 256) {
  const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const side = Math.min(bmp.width, bmp.height);
  const cv = document.createElement('canvas');
  cv.width = cv.height = size;
  cv.getContext('2d').drawImage(bmp, (bmp.width - side) / 2, (bmp.height - side) / 2, side, side, 0, 0, size, size);
  bmp.close && bmp.close();
  return new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error('图片处理失败'))), 'image/jpeg', 0.9));
}

// 本机显示偏好（只存在这台设备的浏览器里）
export function showTimes() {
  try {
    return localStorage.getItem('sc.hideTimes') !== '1';
  } catch {
    return true;
  }
}
export function setShowTimes(on) {
  try {
    localStorage.setItem('sc.hideTimes', on ? '0' : '1');
  } catch {}
  document.body.classList.toggle('hide-times', !on);
}
