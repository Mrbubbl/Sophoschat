// 动效：参考 Material Design 3 的动效规范。
// - 缓动：进入用"强调减速"（先快后慢，像自然停下），退出用"强调加速"（先慢后快，像被带走），位置变化用"标准"。
// - 时长：小控件 100–150ms，菜单/卡片 180–250ms，整页和底部面板 280–400ms；退出比进入短。
// - 只动 transform 和 opacity（不触发重排，低端安卓也流畅）。
// - 尊重系统的"减少动态效果"和设置里的"界面动画"开关。
export const EASE = {
  standard: 'cubic-bezier(0.2, 0, 0, 1)',
  decel: 'cubic-bezier(0.05, 0.7, 0.1, 1)', // 强调减速：进入
  accel: 'cubic-bezier(0.3, 0, 0.8, 0.15)', // 强调加速：退出
  spring: 'cubic-bezier(0.34, 1.56, 0.64, 1)', // 轻微回弹：点赞、表情
};
export const DUR = { fast: 120, short: 180, medium: 280, long: 400 };

const mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
export function motionOn() {
  return document.documentElement.dataset.motion !== 'off' && !(mq && mq.matches);
}

const FRAMES = {
  rise: [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }],
  pop: [{ opacity: 0, transform: 'scale(0.94)' }, { opacity: 1, transform: 'none' }],
  sheet: [{ opacity: 0.6, transform: 'translateY(48px)' }, { opacity: 1, transform: 'none' }],
  fade: [{ opacity: 0 }, { opacity: 1 }],
  forward: [{ opacity: 0, transform: 'translateX(28px)' }, { opacity: 1, transform: 'none' }],
  back: [{ opacity: 0, transform: 'translateX(-28px)' }, { opacity: 1, transform: 'none' }],
  bounce: [{ transform: 'scale(0.5)', opacity: 0 }, { transform: 'scale(1.12)', opacity: 1, offset: 0.6 }, { transform: 'none', opacity: 1 }],
};
const DEFAULTS = {
  rise: [DUR.short + 40, EASE.decel],
  pop: [DUR.short + 60, EASE.decel],
  sheet: [DUR.medium + 40, EASE.decel],
  fade: [DUR.short, EASE.standard],
  forward: [DUR.medium + 20, EASE.decel],
  back: [DUR.medium + 20, EASE.decel],
  bounce: [DUR.short + 80, EASE.standard],
};

// 进入动画。返回 Animation（或 null：动效关闭时直接什么都不做）
export function enter(el, kind = 'rise', opts = {}) {
  if (!el || !el.animate || !motionOn()) return null;
  const [duration, easing] = DEFAULTS[kind] || DEFAULTS.rise;
  return el.animate(FRAMES[kind] || FRAMES.rise, { duration, easing, fill: 'backwards', ...opts });
}

// 退出动画。返回 Promise，结束后再移除元素；动效关闭时立刻 resolve
export function exit(el, kind = 'fade') {
  if (!el || !el.animate || !motionOn()) return Promise.resolve();
  const frames =
    kind === 'sheet'
      ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(32px)' }]
      : kind === 'pop'
        ? [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.96)' }]
        : [{ opacity: 1 }, { opacity: 0 }];
  const a = el.animate(frames, { duration: kind === 'fade' ? DUR.fast : DUR.short, easing: EASE.accel, fill: 'forwards' });
  return a.finished.catch(() => {});
}

// 列表重排（FLIP）：先记录每一项的位置，渲染后让它们从旧位置滑到新位置
export function flipBefore(container, selector, key) {
  const m = new Map();
  if (!motionOn()) return m;
  for (const el of container.querySelectorAll(selector)) m.set(el.dataset[key], el.getBoundingClientRect().top);
  return m;
}
export function flipAfter(container, selector, key, before) {
  if (!before.size || !motionOn()) return;
  for (const el of container.querySelectorAll(selector)) {
    const old = before.get(el.dataset[key]);
    if (old == null) continue;
    const dy = old - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 2 || Math.abs(dy) > 600) continue;
    el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: DUR.medium - 40, easing: EASE.standard });
  }
}
