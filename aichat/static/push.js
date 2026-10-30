// 离线推送（Web Push）的浏览器端：订阅 / 取消 / 状态
import { get, post } from './api.js';

export function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

const b64 = (s) => {
  const pad = '='.repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};
const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export async function pushStatus() {
  if (!pushSupported()) return { supported: false };
  const info = await get('/api/push/key');
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  return { supported: true, serverAvailable: info.available, key: info.key, subscribed: !!sub, permission: Notification.permission };
}

export async function enablePush() {
  const st = await pushStatus();
  if (!st.supported) throw new Error('当前浏览器不支持离线推送');
  if (!st.serverAvailable) throw new Error('服务器未启用离线推送');
  const perm = Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
  if (perm !== 'granted') throw new Error('没有获得通知权限，请在浏览器的站点设置里允许通知');
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64(st.key) });
  const j = sub.toJSON();
  await post('/api/push/subscribe', { endpoint: sub.endpoint, p256dh: j.keys?.p256dh || toB64(sub.getKey('p256dh')), auth: j.keys?.auth || toB64(sub.getKey('auth')) });
}

export async function disablePush() {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (sub) {
    await post('/api/push/unsubscribe', { endpoint: sub.endpoint }).catch(() => {});
    await sub.unsubscribe().catch(() => {});
  }
}
