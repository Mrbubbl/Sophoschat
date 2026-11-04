// 邀请：生成二维码 / 链接，管理已发出的邀请
import { api } from './api.js';
import { S } from './store.js';
import { h, icon, modal, toast, copyText, fmtFull, ask } from './util.js';
import qrcode from './qrcode.js';

const NS = 'http://www.w3.org/2000/svg';

export function qrSvg(text, size = 220) {
  const q = qrcode(0, 'M');
  q.addData(text);
  q.make();
  const n = q.getModuleCount();
  const pad = 2;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${n + pad * 2} ${n + pad * 2}`);
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('shape-rendering', 'crispEdges');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', '邀请二维码');
  const bg = document.createElementNS(NS, 'rect');
  bg.setAttribute('width', '100%');
  bg.setAttribute('height', '100%');
  bg.setAttribute('fill', '#fff');
  svg.append(bg);
  let d = '';
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) d += `M${c + pad} ${r + pad}h1v1h-1z`;
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  path.setAttribute('fill', '#111');
  svg.append(path);
  return { svg, q, n, pad };
}

// 生成 PNG（用于保存图片 / 系统分享）
async function qrPng(text, title) {
  const q = qrcode(0, 'M');
  q.addData(text);
  q.make();
  const n = q.getModuleCount();
  const cell = 10;
  const pad = 4;
  const w = (n + pad * 2) * cell;
  const c = document.createElement('canvas');
  c.width = w;
  c.height = w + 70;
  const g = c.getContext('2d');
  g.fillStyle = '#fff';
  g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = '#111';
  for (let r = 0; r < n; r++) for (let k = 0; k < n; k++) if (q.isDark(r, k)) g.fillRect((k + pad) * cell, (r + pad) * cell, cell, cell);
  g.fillStyle = '#333';
  g.font = '600 22px -apple-system, "PingFang SC", "Noto Sans SC", sans-serif';
  g.textAlign = 'center';
  g.fillText(title, w / 2, w + 38);
  return await new Promise((res) => c.toBlob(res, 'image/png'));
}

const EXPIRY = [
  [1, '1 小时'],
  [24, '1 天'],
  [24 * 7, '7 天'],
  [24 * 30, '30 天'],
];
const USES = [1, 5, 20];

export function openInviteDialog({ roomId = null } = {}) {
  const groups = [...S.rooms.values()].filter((r) => r.type === 'group');
  const exp = h('select', { 'aria-label': '有效期' }, EXPIRY.map(([v, l]) => h('option', { value: String(v), selected: v === 24 }, l)));
  const uses = h('select', { 'aria-label': '可用次数' }, USES.map((v) => h('option', { value: String(v) }, v === 1 ? '仅 1 人' : `最多 ${v} 人`)));
  const grp = h('select', { 'aria-label': '加入群聊' }, h('option', { value: '' }, '不拉进群（只建立私聊）'), groups.map((r) => h('option', { value: String(r.id), selected: r.id === roomId }, r.title || '群聊')));
  const note = h('input', { type: 'text', maxlength: '60', placeholder: '备注，比如“给小王”（可选）', 'aria-label': '备注' });
  const body = h('div', { class: 'invite-dlg' });
  const form = h(
    'div',
    { class: 'form' },
    h('label', null, '有效期', exp),
    h('label', null, '可用次数', uses),
    groups.length ? h('label', null, '注册后加入', grp) : null,
    h('label', null, '备注', note),
    h('button', { class: 'btn primary', type: 'button', onclick: create }, icon('qr', 16), '生成邀请'),
    h('p', { class: 'muted small' }, '对方扫码或打开链接即可注册，不需要开放公开注册。链接只显示这一次，请直接发给对方。')
  );
  body.append(form);
  const close = modal({ title: '邀请成员', body });

  async function create() {
    try {
      const r = await api('POST', '/api/invites', { hours: Number(exp.value), max_uses: Number(uses.value), room_id: grp.value ? Number(grp.value) : null, note: note.value.trim() || null });
      showResult(r.token, r.invite);
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  function showResult(token, inv) {
    const url = `${location.origin}/?invite=${encodeURIComponent(token)}`;
    const { svg } = qrSvg(url, 220);
    const title = inv.room ? `加入「${inv.room.title}」` : '加入 SophosChat';
    const actions = h(
      'div',
      { class: 'row-actions centered' },
      h('button', { class: 'btn', type: 'button', onclick: async () => (await copyText(url), toast('链接已复制')) }, icon('link', 15), '复制链接'),
      h('button', {
        class: 'btn',
        type: 'button',
        onclick: async () => {
          const blob = await qrPng(url, title);
          const a = h('a', { href: URL.createObjectURL(blob), download: 'sophoschat-invite.png' });
          document.body.append(a);
          a.click();
          setTimeout(() => (URL.revokeObjectURL(a.href), a.remove()), 1000);
        },
      }, icon('download', 15), '保存二维码'),
      navigator.share
        ? h('button', {
            class: 'btn primary',
            type: 'button',
            onclick: async () => {
              try {
                const blob = await qrPng(url, title);
                const file = new File([blob], 'sophoschat-invite.png', { type: 'image/png' });
                const data = { title: 'SophosChat 邀请', text: `${S.me.display_name} 邀请你${title}：${url}` };
                if (navigator.canShare && navigator.canShare({ files: [file] })) data.files = [file];
                await navigator.share(data);
              } catch {}
            },
          }, icon('share', 15), '分享')
        : null
    );
    body.replaceChildren(
      h(
        'div',
        { class: 'invite-result' },
        h('div', { class: 'qrbox' }, svg),
        h('b', null, title),
        h('div', { class: 'muted small' }, `${inv.max_uses === 1 ? '仅限 1 人使用' : `最多 ${inv.max_uses} 人使用`} · ${fmtFull(inv.expires_at)} 前有效`),
        h('div', { class: 'invite-url' }, url),
        actions,
        h('button', { class: 'link', type: 'button', onclick: () => (close(), openInviteDialog({ roomId })) }, '再生成一个')
      )
    );
  }
}

const STATUS = { active: '可用', used_up: '已用完', expired: '已过期', revoked: '已撤销' };

export async function renderInviteList(box) {
  box.replaceChildren(h('div', { class: 'loading small' }, '加载中…'));
  try {
    const { invites } = await api('GET', '/api/invites');
    if (!invites.length) return box.replaceChildren(h('p', { class: 'muted small' }, '还没有发出过邀请。'));
    box.replaceChildren(
      ...invites.slice(0, 30).map((v) =>
        h(
          'div',
          { class: 'inv-item ' + v.status },
          h(
            'div',
            { class: 'grow' },
            h('div', null, h('b', null, v.note || (v.room ? `进群「${v.room.title}」` : '邀请')), h('span', { class: 'tag' }, STATUS[v.status] || v.status)),
            h(
              'div',
              { class: 'muted small' },
              `${v.uses}/${v.max_uses} 人已使用 · ${v.status === 'active' ? fmtFull(v.expires_at) + ' 前有效' : '创建于 ' + fmtFull(v.created_at)}` + (S.me.role === 'owner' && !v.mine && v.created_by ? ` · ${v.created_by.display_name} 发出` : '')
            ),
            v.used_by.length ? h('div', { class: 'muted small' }, '加入的人：' + v.used_by.map((u) => u.display_name).join('、')) : null
          ),
          v.status === 'active'
            ? h('button', {
                class: 'btn small danger-outline',
                type: 'button',
                onclick: async () => {
                  if (!(await ask('撤销后这个链接和二维码立即失效。', { ok: '撤销', danger: true }))) return;
                  try {
                    await api('DELETE', `/api/invites/${v.id}`);
                    renderInviteList(box);
                  } catch (e) {
                    toast(e.message, 'err');
                  }
                },
              }, '撤销')
            : null
        )
      )
    );
  } catch (e) {
    box.replaceChildren(h('p', { class: 'muted small' }, e.message));
  }
}
