// emoji 面板：纯前端、无外部依赖。最近使用保存在本机浏览器。
import { h } from './util.js';

export const QUICK = ['👍', '❤️', '😂', '😮', '🙏', '🎉'];

const CATS = [
  ['😀', '表情', '😀 😃 😄 😁 😆 😅 🤣 😂 🙂 🙃 😉 😊 😇 🥰 😍 🤩 😘 😗 😚 😙 😋 😛 😜 🤪 😝 🤑 🤗 🤭 🤫 🤔 🤐 🤨 😐 😑 😶 😏 😒 🙄 😬 😮‍💨 🤥 😌 😔 😪 🤤 😴 😷 🤒 🤕 🤢 🤮 🤧 🥵 🥶 🥴 😵 🤯 🤠 🥳 😎 🤓 🧐 😕 😟 🙁 ☹️ 😮 😯 😲 😳 🥺 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 😞 😓 😩 😫 🥱 😤 😡 😠 🤬 😈 👿 💀 ☠️ 💩 🤡 👻 👽 🤖'],
  ['👋', '手势', '👋 🤚 🖐️ ✋ 🖖 👌 🤌 🤏 ✌️ 🤞 🤟 🤘 🤙 👈 👉 👆 👇 ☝️ 👍 👎 ✊ 👊 🤛 🤜 👏 🙌 👐 🤲 🤝 🙏 ✍️ 💅 💪 🦾 👀 👂 👃 🧠 🫶 🫡 🫠 🙈 🙉 🙊'],
  ['❤️', '符号', '❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞 💓 💗 💖 💘 💝 💯 💢 💥 💫 💦 💨 💤 ✅ ❌ ❓ ❗ ‼️ ⭕ ⚠️ 🚫 ✔️ ➕ ➖ ➡️ ⬅️ ⬆️ ⬇️ 🔥 ⭐ 🌟 ✨ ⚡ 🎉 🎊 🏆 🎯'],
  ['🐶', '动物', '🐶 🐱 🐭 🐹 🐰 🦊 🐻 🐼 🐨 🐯 🦁 🐮 🐷 🐸 🐵 🐔 🐧 🐦 🦆 🦉 🐺 🐴 🦄 🐝 🦋 🐌 🐞 🐢 🐍 🐙 🦀 🐠 🐬 🐳 🌸 🌹 🌻 🌲 🍀 🌈 ☀️ 🌙 ⛅ ☔ ❄️'],
  ['🍎', '食物', '🍎 🍊 🍋 🍌 🍉 🍇 🍓 🍒 🍑 🥭 🍍 🥝 🍅 🥑 🌽 🥕 🍞 🧀 🍳 🥞 🥓 🍔 🍟 🍕 🌭 🌮 🍜 🍝 🍣 🍱 🍚 🍙 🍦 🍰 🎂 🍩 🍪 🍫 🍿 ☕ 🍵 🍺 🍷 🥂 🍾'],
  ['⚽', '活动', '⚽ 🏀 🏈 ⚾ 🎾 🏐 🎱 🏓 🏸 🥊 🎮 🎲 🎯 🎳 🎤 🎧 🎸 🎹 🎬 🎨 📚 ✈️ 🚗 🚌 🚄 🚲 🏠 🏢 🏥 🏖️ ⛰️ 🗺️'],
  ['💡', '物品', '💡 📱 💻 🖥️ ⌨️ 📷 📎 📌 ✂️ 🔑 🔒 🔓 🔔 📢 📦 📝 📄 📊 📈 📉 📅 ⏰ ⌛ 💰 💳 🧾 🔧 🔨 🧪 💊 🩺 🧰 🛒 🎁 🧧'],
];

const RECENT_KEY = 'aichat.emoji.recent';
function loadRecent() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]').slice(0, 16);
  } catch {
    return [];
  }
}
function pushRecent(e) {
  try {
    const list = [e, ...loadRecent().filter((x) => x !== e)].slice(0, 16);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch {}
}

// onPick(emoji) 在点选时调用；按钮不抢焦点，避免输入框失焦导致键盘闪退
export function buildEmojiPanel(onPick, { compact = false } = {}) {
  const grid = h('div', { class: 'emoji-grid', role: 'listbox' });
  const tabs = h('div', { class: 'emoji-tabs' });
  const noFocus = (e) => e.preventDefault();
  const cats = [['🕘', '最近', null], ...CATS];
  let cur = loadRecent().length ? 0 : 1;
  const show = (i) => {
    cur = i;
    const list = i === 0 ? loadRecent() : cats[i][2].split(' ');
    grid.replaceChildren(
      ...(list.length
        ? list.map((e) => h('button', { type: 'button', class: 'emoji', role: 'option', 'aria-label': e, onpointerdown: noFocus, onmousedown: noFocus, onclick: () => (pushRecent(e), onPick(e)) }, e))
        : [h('div', { class: 'empty small' }, '还没有最近使用的表情')])
    );
    grid.scrollTop = 0;
    tabs.replaceChildren(...cats.map((c, k) => h('button', { type: 'button', class: 'etab' + (k === cur ? ' on' : ''), title: c[1], 'aria-label': c[1], onpointerdown: noFocus, onmousedown: noFocus, onclick: () => show(k) }, c[0])));
  };
  show(cur);
  return h('div', { class: 'emoji-panel' + (compact ? ' compact' : '') }, grid, tabs);
}
