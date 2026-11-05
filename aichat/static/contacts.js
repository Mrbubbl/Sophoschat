// A private address book, independent of conversations and server account management.
import { get, post, del } from './api.js';
import { S, emit, on } from './store.js';
import { h, avatar, modal, toast, ask } from './util.js';

let box, list, search;
let loadGeneration = 0;
export async function loadContacts() {
  const owner = S.me?.id;
  const generation = ++loadGeneration;
  const d = await get('/api/contacts');
  if (S.me?.id !== owner || generation !== loadGeneration) return;
  S.users = d.contacts;
  renderContacts();
  emit('contacts:changed');
}

export function initContacts(container) {
  search = h('input', { type: 'search', placeholder: '搜索我的联系人', 'aria-label': '搜索我的联系人', oninput: renderContacts });
  list = h('div', { class: 'contact-list' });
  box = h('section', { class: 'contacts-pane', hidden: true, 'aria-label': '联系人列表' },
    h('div', { class: 'contact-toolbar' }, search, h('button', { class: 'btn primary', type: 'button', onclick: openAddContact }, '添加联系人')), list);
  container.append(box);
  on('contacts:reload', () => loadContacts().catch(e => toast(e.message, 'err')));
  on('resync', () => loadContacts().catch(() => {}));
  on('user:updated', u => {
    S.users = S.users.map(x => x.id === u.id ? { ...x, ...u } : x);
    renderContacts();
  });
}

export function showContacts(show) {
  if (box) box.hidden = !show;
  if (show) renderContacts();
}

function renderContacts() {
  if (!list) return;
  const q = (search.value || '').trim().toLocaleLowerCase();
  const users = S.users.filter(u => `${u.display_name} ${u.username}`.toLocaleLowerCase().includes(q))
    .sort((a,b) => a.display_name.localeCompare(b.display_name) || a.username.localeCompare(b.username) || a.id-b.id);
  list.replaceChildren(...(users.length ? users.map(u => h('button', { class: 'contact-item', type: 'button', onclick: () => openContact(u) },
    avatar(u, 36), h('span', null, h('b', null, u.display_name), h('small', { class: 'muted' }, '@' + u.username + (u.disabled ? ' · 账号已停用' : '')))))
    : [h('div', { class: 'empty' }, q ? '没有匹配的联系人' : '还没有联系人，按用户名添加一位。')]));
}

export function openAddContact() {
  const input = h('input', { type: 'text', placeholder: '输入完整用户名', 'aria-label': '查找联系人用户名', maxlength: 32, autocomplete: 'off' });
  const result = h('div', { class: 'contact-result', role: 'status', 'aria-live': 'polite' });
  let generation = 0;
  input.addEventListener('input', () => { generation++; result.replaceChildren(); });
  const find = h('button', { class: 'btn', type: 'submit' }, '查找');
  const form = h('form', { class: 'form', onsubmit: async e => {
    e.preventDefault();
    const mine = ++generation;
    find.disabled = true;
    result.textContent = '正在查找…';
    try {
      const { user } = await get('/api/users/lookup?username=' + encodeURIComponent(input.value.trim()));
      if (mine !== generation) return;
      const saved = S.users.some(u => u.id === user.id);
      const add = h('button', { type: 'button', class: 'btn primary', onclick: async () => {
        add.disabled = true;
        try {
          if (!saved) { await post('/api/contacts', { user_id: user.id }); await loadContacts(); toast('已添加联系人'); }
          // Push the replacement layer before retiring this one: history.go()
          // is asynchronous, so closing first can pop the new profile's entry.
          openContact(user); close();
        } catch (e) { toast(e.message, 'err'); add.disabled = false; }
      } }, saved ? '查看联系人' : '添加联系人');
      result.replaceChildren(h('div', { class: 'who' }, avatar(user, 40), h('div', null, h('b', null, user.display_name), h('p', { class: 'muted' }, '@' + user.username))), add);
    } catch (e) { if (mine === generation) result.textContent = e.message; }
    finally { find.disabled = false; }
  } }, input, find, result);
  const close = modal({ title: '添加联系人', body: form });
  input.focus();
}

export function openContact(initial) {
  if (!initial || initial.id === S.me.id || initial.role === 'agent') return;
  const body = h('div', { class: 'contact-profile' });
  let busy = false;
  const render = () => {
    const saved = S.users.find(u => u.id === initial.id);
    const u = saved || initial;
    body.replaceChildren(h('div', { class: 'who' }, avatar(u, 56), h('div', null, h('b', null, u.display_name), h('p', { class: 'muted' }, '@' + u.username))),
      h('p', { class: 'muted small' }, u.disabled ? '账号已停用；已有聊天记录保留。' : saved ? '已在你的联系人列表中' : '添加到你的联系人，不会自动互加。'));
    const action = (text, fn, disabled = false) => h('button', { type: 'button', class: 'btn', disabled: disabled || busy, onclick: async () => {
      if (busy) return;
      busy = true; render();
      try { await fn(); } catch (e) { toast(e.message, 'err'); }
      finally { busy = false; render(); }
    } }, text);
    if (!saved) body.append(action('添加联系人', async () => {
      await post('/api/contacts', { user_id: u.id }); await loadContacts(); toast('已添加联系人');
    }, u.disabled));
    else {
      body.append(action('发消息', async () => {
        const { room } = await post('/api/rooms', { type: 'dm', user_id: u.id, include_agent: true });
        S.rooms.set(room.id, room); emit('rooms:render'); emit('open-room', { id: room.id }); close();
      }, u.disabled));
      body.append(action('移除联系人', async () => {
        if (!await ask('仅从你的联系人列表移除，聊天记录保留。', { ok: '移除', danger: true })) return;
        await del('/api/contacts/' + u.id); await loadContacts(); close(); toast('已移除联系人');
      }));
    }
  };
  const subscriptions = [];
  const close = modal({ title: '联系人资料', body, onClose: () => subscriptions.forEach(fn => fn()) });
  subscriptions.push(on('contacts:changed', render), on('user:updated', render));
  render();
}
