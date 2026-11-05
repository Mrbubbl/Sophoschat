// 首屏前同步设定界面风格，避免先闪一下另一种风格。服务器上的偏好加载后会再校正（appearance.js）。
try {
  document.documentElement.dataset.ui = localStorage.getItem('sophos-ui') === 'classic' ? 'classic' : 'nothing';
} catch {
  document.documentElement.dataset.ui = 'nothing';
}
