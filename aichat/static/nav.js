// 返回键 / 返回手势：让安卓的返回键（和 iPhone 的边缘右滑）先退出弹窗、再从聊天退回列表，而不是直接退出到桌面。
//
// 做法：每打开一层界面（手机上的“聊天页 / 回顾面板”视图，以及设置、预览大图等弹层），就往浏览器历史里压一条记录；
// 用户按返回时，浏览器弹出这条记录，我们就关闭对应的那一层。界面自己关闭某一层（点 × / 点返回箭头）时，
// 把对应记录标记为作废，并回退历史，保持两边一致。只有在最底层（聊天列表）再按返回，才会离开应用。
const entries = []; // { t: 'v' | 'l', dead, close }
let ignore = 0;
let onBackView = () => {};
let ready = false;

export function initNav(handlers) {
  if (ready) return;
  ready = true;
  onBackView = handlers.onBackView;
  try {
    history.replaceState({ sc: 0 }, '');
  } catch {}
  window.addEventListener('popstate', () => {
    if (ignore > 0) {
      ignore--;
      return;
    }
    const e = entries.pop();
    if (!e) return; // 已经在最底层：交给浏览器（退出应用）
    if (e.dead) return history.back(); // 这一层早就关掉了，继续往回退
    e.dead = true;
    if (e.t === 'l') e.close && e.close();
    else onBackView();
  });
}

function push(e) {
  entries.push(e);
  try {
    history.pushState({ sc: entries.length }, '');
  } catch {}
}

// 把已经关闭的顶层记录从历史里退掉（一次性回退 n 步）
function trim() {
  let n = 0;
  while (entries.length && entries[entries.length - 1].dead) {
    entries.pop();
    n++;
  }
  if (n) {
    ignore++;
    try {
      history.go(-n);
    } catch {
      ignore--;
    }
  }
}

// 打开一层弹层。close()：用户按返回时用来关闭这一层；返回的函数：这一层被界面自己关闭时调用。
export function openLayer(close) {
  const e = { t: 'l', dead: false, close };
  push(e);
  return () => {
    if (e.dead) return;
    e.dead = true;
    trim();
  };
}

// 手机上的视图层级：0 列表，1 聊天，2 回顾面板。want 变化时增删历史记录。
export function syncView(want) {
  const live = () => entries.filter((e) => e.t === 'v' && !e.dead);
  while (live().length < want) push({ t: 'v', dead: false });
  while (live().length > want) {
    const top = live().pop();
    top.dead = true;
  }
  trim();
}
