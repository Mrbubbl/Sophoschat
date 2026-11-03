// 链接预览卡片：消息里的网址由服务器抓取标题/摘要/封面，所有人看到同一张卡片。
import { api } from './api.js';
import { S } from './store.js';
import { h, icon } from './util.js';

const URL_RE = /https?:\/\/[^\s<>"'）)\]]+/g;
const cache = new Map(); // url -> Promise<preview|null>，同一页面里同一个链接只请求一次
const dismissed = (() => {
  try {
    return new Set(JSON.parse(localStorage.getItem('lp-dismissed') || '[]'));
  } catch {
    return new Set();
  }
})();

function saveDismissed() {
  try {
    localStorage.setItem('lp-dismissed', JSON.stringify([...dismissed].slice(-400)));
  } catch {}
}

// 取消息里前两个值得预览的链接：忽略代码、本站链接，去掉结尾标点
export function previewUrls(body, max = 2) {
  const text = String(body || '').replace(/```[\s\S]*?```/g, ' ').replace(/`[^`\n]*`/g, ' ');
  const out = [];
  for (let u of text.match(URL_RE) || []) {
    u = u.replace(/[.,;:!?，。；：！？、]+$/, '');
    try {
      const x = new URL(u);
      if (x.origin === location.origin) continue;
      if (!out.includes(u)) out.push(u);
    } catch {}
    if (out.length >= max) break;
  }
  return out;
}

function load(url) {
  if (!cache.has(url)) {
    cache.set(
      url,
      api('GET', '/api/link-preview?url=' + encodeURIComponent(url))
        .then((r) => r.preview)
        .catch(() => null)
    );
    // 失败/没有预览的结果过一会儿允许重试
    cache.get(url).then((p) => !p && setTimeout(() => cache.delete(url), 5 * 60 * 1000));
  }
  return cache.get(url);
}

// 返回要插入到消息气泡里的容器；onLayout 在卡片/图片加载完成后调用（用于保持滚动位置）
export function linkCards(m, onLayout) {
  if (!S.prefs || S.prefs.link_preview === false) return null;
  const urls = previewUrls(m.body).filter((u) => !dismissed.has(`${m.id}|${u}`));
  if (!urls.length) return null;
  const box = h('div', { class: 'lcards' });
  for (const url of urls) {
    const slot = h('div', { class: 'lcard-slot', hidden: true });
    box.append(slot);
    load(url).then((p) => {
      if (!p) return;
      slot.replaceChildren(card(p, url, m, slot, onLayout));
      slot.hidden = false;
      onLayout && onLayout();
    });
  }
  return box;
}

function card(p, url, m, slot, onLayout) {
  let host = '';
  try {
    host = new URL(url).hostname.replace(/^www\./, '');
  } catch {}
  const img = p.image_url
    ? h('img', { class: 'lc-img', src: p.image_url, alt: '', loading: 'lazy', onload: () => onLayout && onLayout(), onerror: (e) => (e.target.remove(), onLayout && onLayout()) })
    : null;
  const a = h(
    'a',
    { class: 'lcard' + (p.kind === 'x' ? ' x' : ''), href: url, target: '_blank', rel: 'noopener noreferrer nofollow' },
    img,
    h('div', { class: 'lc-body' }, h('div', { class: 'lc-site' }, icon('link', 12), p.site || host), p.title ? h('div', { class: 'lc-title' }, p.title) : null, p.description ? h('div', { class: 'lc-desc' }, p.description) : null)
  );
  const close = h(
    'button',
    {
      class: 'lc-x',
      type: 'button',
      title: '不再显示这个预览',
      'aria-label': '不再显示这个预览',
      onclick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        dismissed.add(`${m.id}|${url}`);
        saveDismissed();
        slot.remove();
        onLayout && onLayout();
      },
    },
    icon('close', 12)
  );
  return h('div', { class: 'lc-wrap' }, a, close);
}
