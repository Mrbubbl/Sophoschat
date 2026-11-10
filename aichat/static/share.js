// 安卓 App：从系统“分享”进来的文字/图片/文件 → 选一个聊天 → 发送
import { api } from './api.js';
import { S, on, emit, roomTitle } from './store.js';
import { h, modal, toast, avatar } from './util.js';

let ready = false;

export function initShare() {
  window.addEventListener('sophos:share', () => ready && take());
  on('app:ready', () => { ready = true; take(); });
}

function take() {
  const data = window.__sophosShare;
  if (!data) return;
  window.__sophosShare = null;
  const files = (data.files || []).map((f) => {
    const bin = atob(f.data);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return new File([buf], f.name || 'shared', { type: f.mime || 'application/octet-stream' });
  });
  const text = (data.text || '').trim();
  if (!files.length && !text) return;
  if (data.skipped) toast(`有 ${data.skipped} 个文件太大，没有带过来`, 'err');
  pick(text, files);
}

function pick(text, files) {
  const what = [files.length ? `${files.length} 个${files.every((f) => f.type.startsWith('image/')) ? '图片' : '文件'}` : '', text ? '文字' : ''].filter(Boolean).join(' + ');
  const rooms = [...S.rooms.values()].filter((r) => !r.archived).sort((a, b) => (b.pinned - a.pinned) || (b.last_activity || '').localeCompare(a.last_activity || ''));
  let close;
  const list = h('div', { class: 'share-list' }, rooms.map((r) => {
    const other = r.type === 'dm' ? r.members.find((m) => m.id !== S.me.id) : null;
    return h('button', { class: 'share-room', type: 'button', onclick: () => { close(); send(r, text, files); } },
      other ? avatar(other, 36) : h('span', { class: 'share-ic' }, (roomTitle(r) || '#').slice(0, 1)),
      h('span', { class: 'share-name' }, roomTitle(r)));
  }));
  const preview = text ? h('p', { class: 'share-text' }, text.length > 120 ? text.slice(0, 120) + '…' : text) : null;
  close = modal({ title: '发送到…', sheet: true, body: h('div', { class: 'share' }, h('div', { class: 'share-head' }, h('b', null, '发送给'), h('span', { class: 'muted small' }, what)), preview, list) });
}

async function send(r, text, files) {
  emit('open-room', { id: r.id });
  try {
    for (const f of files) {
      const fd = new FormData();
      fd.append('file', f, f.name);
      const { attachment } = await api('POST', `/api/rooms/${r.id}/attachments`, fd);
      await api('POST', `/api/rooms/${r.id}/messages`, { body: '', attachment_id: attachment.id, client_msg_id: crypto.randomUUID() });
    }
    if (text) await api('POST', `/api/rooms/${r.id}/messages`, { body: text, client_msg_id: crypto.randomUUID() });
    toast('已发送');
  } catch (e) {
    toast('发送失败：' + e.message, 'err');
  }
}
