// 最小 Service Worker：缓存静态外壳，使“添加到主屏幕”后可离线打开界面；接口与消息永远走网络。
const CACHE = 'aichat-shell-v3';
const SHELL = ['/', '/static/style.css', '/static/main.js', '/static/util.js', '/static/store.js', '/static/api.js', '/static/ws.js', '/static/rooms.js', '/static/chatview.js', '/static/panel.js', '/static/settings.js', '/static/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws' || url.pathname.endsWith('.md')) return; // 不缓存业务数据与附件
  // 外壳：网络优先，失败时回退缓存
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && (url.pathname === '/' || url.pathname.startsWith('/static/'))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});

// ---- 离线推送
self.addEventListener('push', (e) => {
  let d = {};
  try {
    d = e.data ? e.data.json() : {};
  } catch {
    d = { title: '新消息', body: e.data ? e.data.text() : '' };
  }
  try {
    if (typeof d.badge === 'number' && self.navigator && self.navigator.setAppBadge) self.navigator.setAppBadge(d.badge);
  } catch {}
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => {
      // 窗口正在前台显示时由页面自己处理，不重复弹通知
      if (cs.some((c) => c.visibilityState === 'visible' && c.focused)) return;
      return self.registration.showNotification(d.title || '新消息', {
        body: d.body || '',
        tag: d.tag || undefined,
        renotify: true,
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
        requireInteraction: !!d.call,
        data: { room_id: d.room_id },
      });
    })
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const rid = e.notification.data && e.notification.data.room_id;
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => {
      const c = cs[0];
      if (c) {
        c.postMessage({ type: 'open-room', room_id: rid });
        return c.focus();
      }
      return self.clients.openWindow(rid ? '/?room=' + rid : '/');
    })
  );
});
