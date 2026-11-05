// 外观：主题色、字号、聊天背景（每个聊天可单独设置，偏好跨设备同步）
import { api } from './api.js';
import { S, emit } from './store.js';
import { h, icon, modal, toast } from './util.js';

export const ACCENTS = [
  { id: 'teal', name: '青绿', c: '#0f766e' },
  { id: 'indigo', name: '靛蓝', c: '#4f46e5' },
  { id: 'blue', name: '海蓝', c: '#2563eb' },
  { id: 'violet', name: '紫罗兰', c: '#7c3aed' },
  { id: 'rose', name: '玫红', c: '#e11d48' },
  { id: 'orange', name: '橙', c: '#ea580c' },
  { id: 'green', name: '翠绿', c: '#16a34a' },
  { id: 'slate', name: '石墨', c: '#475569' },
];
export const UIS = [
  ['nothing', 'Nothing'],
  ['classic', '经典'],
];
export const FONTS = [
  ['s', '小'],
  ['m', '标准'],
  ['l', '大'],
  ['xl', '特大'],
];
export const PRESETS = [
  { id: 'dots', name: '圆点' },
  { id: 'grid', name: '方格' },
  { id: 'waves', name: '波纹' },
  { id: 'paper', name: '纸张' },
  { id: 'aurora', name: '极光' },
  { id: 'dusk', name: '暮色' },
  { id: 'mint', name: '薄荷' },
  { id: 'night', name: '夜空' },
];
export const COLORS = ['#ece5d8', '#dde7f2', '#e1eee4', '#f2e1e6', '#e9e4f5', '#2a2f36', '#1d3a4a', '#3b2f2a'];

const root = document.documentElement;

export function applyPrefs(p) {
  if (!p) return;
  S.prefs = p;
  const ui = p.ui === 'classic' ? 'classic' : 'nothing';
  root.dataset.ui = ui;
  try { localStorage.setItem('sophos-ui', ui); } catch {}
  root.dataset.accent = ACCENTS.some((a) => a.id === p.accent) ? p.accent : 'teal';
  root.dataset.font = p.font || 'm';
  root.dataset.dim = p.dim ? '1' : '0';
  root.dataset.motion = p.motion === false ? 'off' : 'on';
  emit('prefs', p);
}

export async function savePrefs(patch) {
  try {
    const r = await api('PUT', '/api/me/prefs', patch);
    applyPrefs(r.prefs);
    return r.prefs;
  } catch (e) {
    toast(e.message, 'err');
    return null;
  }
}

export function effectiveBg(r) {
  return (r && r.bg) || (S.prefs && S.prefs.bg) || null;
}

// 把背景画到某个元素上（聊天区或预览块）
export function paintBg(el, bg) {
  const isColor = typeof bg === 'string' && bg.startsWith('#');
  const img = bg === 'image' && S.prefs && S.prefs.bg_image_url;
  el.dataset.bg = bg && !isColor ? (bg === 'image' && !img ? '' : bg) : isColor ? 'color' : '';
  el.style.backgroundColor = isColor ? bg : '';
  el.style.backgroundImage = img ? `url("${S.prefs.bg_image_url}")` : '';
  if (isColor) el.dataset.tone = lum(bg) < 0.45 ? 'dark' : 'light';
  else el.dataset.tone = bg === 'night' || bg === 'image' ? 'dark' : '';
}

function lum(hex) {
  const n = parseInt(hex.slice(1), 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
}

// 背景选择器；value 为 null 表示"跟随默认"
export function bgPicker(value, onPick, { allowDefault = true } = {}) {
  const box = h('div', { class: 'bgpick' });
  const sw = (bg, label) => {
    const b = h('button', { type: 'button', class: 'bgsw' + ((value ?? null) === bg ? ' on' : ''), title: label, 'aria-label': label, 'aria-pressed': String((value ?? null) === bg) });
    const prev = h('span', { class: 'bgprev' });
    if (bg) paintBg(prev, bg);
    else prev.classList.add('def');
    b.append(prev, h('small', null, label));
    b.addEventListener('click', () => {
      value = bg;
      [...box.querySelectorAll('.bgsw')].forEach((x) => x.classList.toggle('on', x === b));
      onPick(bg);
    });
    return b;
  };
  if (allowDefault) box.append(sw(null, '跟随默认'));
  else box.append(sw(null, '无'));
  PRESETS.forEach((p) => box.append(sw(p.id, p.name)));
  COLORS.forEach((c) => box.append(sw(c, '纯色')));
  if (S.prefs && S.prefs.bg_image_url) box.append(sw('image', '我的图片'));
  return box;
}

export async function uploadBgImage(file) {
  if (!file) return null;
  const fd = new FormData();
  fd.append('file', await shrink(file), 'bg.jpg');
  try {
    const r = await api('POST', '/api/me/background', fd);
    applyPrefs(r.prefs);
    return r.prefs;
  } catch (e) {
    toast(e.message, 'err');
    return null;
  }
}

// 背景图压到长边 1600，避免手机原图太大
async function shrink(file) {
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
    const c = document.createElement('canvas');
    c.width = Math.round(bmp.width * k);
    c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    return await new Promise((res) => c.toBlob((b) => res(b || file), 'image/jpeg', 0.85));
  } catch {
    return file;
  }
}

export function pickImage(onFile) {
  const inp = h('input', { type: 'file', accept: 'image/*', hidden: true });
  inp.addEventListener('change', () => {
    const f = inp.files[0];
    inp.remove();
    f && onFile(f);
  });
  document.body.append(inp);
  inp.click();
}

// 某个聊天的背景（只对自己生效）
export function openRoomBg(r) {
  const body = h('div', { class: 'bgdlg' });
  const render = () => {
    const prev = h('div', { class: 'bgbig' }, h('div', { class: 'fake other' }, '这个背景怎么样？'), h('div', { class: 'fake own' }, '看着舒服就行 👍'));
    paintBg(prev, effectiveBg(S.rooms.get(r.id)));
    body.replaceChildren(
      prev,
      bgPicker(S.rooms.get(r.id).bg, async (bg) => {
        try {
          const res = await api('PUT', `/api/rooms/${r.id}/background`, { bg });
          emit('room:update', res.room);
          paintBg(prev, effectiveBg(res.room));
        } catch (e) {
          toast(e.message, 'err');
        }
      }),
      h(
        'div',
        { class: 'row-actions' },
        h('button', { class: 'btn small', type: 'button', onclick: () => pickImage(async (f) => (await uploadBgImage(f)) && render()) }, icon('image', 15), S.prefs && S.prefs.bg_image_url ? '更换我的图片' : '上传图片'),
        h('span', { class: 'muted small' }, '只对你自己生效，对方看到的是他自己的设置。')
      )
    );
  };
  render();
  modal({ title: '聊天背景', body });
}
