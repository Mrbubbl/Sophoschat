// 一对一语音 / 视频通话（WebRTC）。信令走现有 WebSocket，音视频点对点传输；需要时经 TURN 中继。
import { get } from './api.js';
import { S, on, room, otherHuman } from './store.js';
import { wsSend } from './ws.js';
import { h, icon, iconBtn, avatar, toast } from './util.js';
import { fmtDur } from './voice.js';

const RING_MS = 50000;
let wakeLock = null;
let cur = null; // 当前通话 { id, roomId, peer, media, role, state, pc, local, remote, ... }
let overlay = null;
let ringer = null;

export function callSupported() {
  return !!(window.RTCPeerConnection && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);
}

export function canCall(r) {
  return !!r && r.type === 'dm' && callSupported();
}

export function initCalls() {
  on('call', onSignal);
  on('online', (ok) => !ok && cur && end('网络中断', true));
  window.addEventListener('pagehide', () => cur && sendSig('hangup'));
}

const sendSig = (kind, data) => cur && wsSend({ type: 'call', kind, room_id: cur.roomId, call_id: cur.id, media: cur.media, data });
const newId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).slice(0, 36);

// ------------------------------------------------------------------ 外呼
export async function startCall(roomId, media) {
  if (cur) return toast('正在通话中');
  const r = room(roomId);
  if (!canCall(r)) return toast('这个会话不能通话');
  const peer = otherHuman(r);
  cur = { id: newId(), roomId, peer, media, role: 'caller', state: 'calling' };
  showOverlay();
  try {
    cur.local = await getMedia(media);
  } catch (e) {
    return end(mediaError(e), false);
  }
  attachLocal();
  detectFlip();
  grabWakeLock();
  sendSig('invite');
  cur.ringTimer = setTimeout(() => cur && cur.state === 'calling' && end('对方没有接听', true, 'cancel'), RING_MS);
  startRingback();
}

// ------------------------------------------------------------------ 信令
async function onSignal(d) {
  if (d.kind === 'unsupported') return end('这个会话不支持通话', false);
  if (d.kind === 'invite') {
    if (cur) return wsSend({ type: 'call', kind: 'busy', room_id: d.room_id, call_id: d.call_id });
    const r = room(d.room_id);
    cur = { id: d.call_id, roomId: d.room_id, peer: r ? otherHuman(r) : { id: d.from, display_name: '对方' }, media: d.media, role: 'callee', state: 'ringing' };
    showOverlay();
    startRingtone();
    cur.ringTimer = setTimeout(() => cur && cur.state === 'ringing' && end('未接来电', false), RING_MS + 5000);
    return;
  }
  if (!cur || d.call_id !== cur.id) {
    if (d.kind === 'busy' && !cur) toast('对方正忙');
    return;
  }
  switch (d.kind) {
    case 'busy':
      return end('对方正忙', false);
    case 'decline':
      return end('对方拒绝了通话', false);
    case 'cancel':
      return end('对方已取消', false);
    case 'hangup':
      return end('通话已结束', false);
    case 'accept':
      if (cur.role !== 'caller') return;
      stopTones();
      clearTimeout(cur.ringTimer);
      cur.state = 'connecting';
      render();
      await makePc();
      {
        const offer = await cur.pc.createOffer();
        await cur.pc.setLocalDescription(offer);
        sendSig('offer', { sdp: cur.pc.localDescription.sdp, type: 'offer' });
      }
      return;
    case 'offer':
      if (cur.role !== 'callee') return;
      await makePc();
      await cur.pc.setRemoteDescription(d.data);
      await flushIce();
      {
        const ans = await cur.pc.createAnswer();
        await cur.pc.setLocalDescription(ans);
        sendSig('answer', { sdp: cur.pc.localDescription.sdp, type: 'answer' });
      }
      return;
    case 'answer':
      if (cur.pc) {
        await cur.pc.setRemoteDescription(d.data);
        await flushIce();
      }
      return;
    case 'ice':
      if (cur.pc && cur.pc.remoteDescription) cur.pc.addIceCandidate(d.data).catch(() => {});
      else (cur.pendingIce ||= []).push(d.data);
  }
}

async function flushIce() {
  for (const c of cur.pendingIce || []) await cur.pc.addIceCandidate(c).catch(() => {});
  cur.pendingIce = [];
}

// ------------------------------------------------------------------ 接听 / 拒绝 / 挂断
async function accept() {
  if (!cur || cur.state !== 'ringing') return;
  stopTones();
  clearTimeout(cur.ringTimer);
  cur.state = 'connecting';
  render();
  try {
    cur.local = await getMedia(cur.media);
  } catch (e) {
    return end(mediaError(e), true, 'decline');
  }
  attachLocal();
  detectFlip();
  grabWakeLock();
  sendSig('accept');
}

function decline() {
  end('', true, 'decline');
}

function hangup() {
  const kind = cur && cur.state === 'calling' ? 'cancel' : cur && cur.state === 'ringing' ? 'decline' : 'hangup';
  end('', true, kind);
}

function end(msg, notify, kind = 'hangup') {
  if (!cur) return;
  const c = cur;
  if (notify) sendSig(kind);
  cur = null;
  clearTimeout(c.ringTimer);
  clearInterval(c.timer);
  stopTones();
  try {
    c.pc && c.pc.close();
  } catch {}
  for (const s of [c.local, c.remote]) s && s.getTracks().forEach((t) => t.stop());
  overlay && overlay.remove();
  overlay = null;
  releaseWakeLock();
  if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.setActionHandler('enterpictureinpicture', null);
    } catch {}
  }
  if (msg) toast(msg);
}

// ------------------------------------------------------------------ WebRTC
async function getMedia(media) {
  return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: media === 'video' ? { facingMode: 'user', width: { ideal: 960 }, height: { ideal: 540 } } : false });
}

function mediaError(e) {
  if (e && e.name === 'NotAllowedError') return '没有麦克风/摄像头权限，请在浏览器的站点设置里允许';
  if (e && e.name === 'NotFoundError') return '没有找到麦克风或摄像头';
  return '无法打开麦克风或摄像头';
}

async function makePc() {
  let cfg = { ice_servers: [{ urls: 'stun:stun.l.google.com:19302' }] };
  try {
    cfg = await get('/api/call/config');
  } catch {}
  const pc = new RTCPeerConnection({ iceServers: cfg.ice_servers });
  cur.pc = pc;
  cur.turn = cfg.turn;
  for (const t of cur.local.getTracks()) pc.addTrack(t, cur.local);
  pc.onicecandidate = (e) => e.candidate && sendSig('ice', e.candidate.toJSON());
  pc.ontrack = (e) => {
    cur.remote = e.streams[0];
    attachRemote();
  };
  pc.onconnectionstatechange = () => {
    if (!cur || cur.pc !== pc) return;
    if (pc.connectionState === 'connected') {
      if (cur.state !== 'connected') {
        cur.state = 'connected';
        cur.t0 = Date.now();
        cur.timer = setInterval(tick, 500);
        armAutoPip();
        render();
      }
    } else if (pc.connectionState === 'failed') {
      end(cur.turn ? '连接失败' : '连接失败：网络不允许直连，需要在服务器上配置 TURN 中继', true);
    } else if (pc.connectionState === 'disconnected') {
      cur.state = 'reconnecting';
      render();
      clearTimeout(cur.dcTimer);
      cur.dcTimer = setTimeout(() => cur && cur.pc === pc && pc.connectionState !== 'connected' && end('通话中断', true), 12000);
    }
  };
}

// ------------------------------------------------------------------ 界面
function showOverlay() {
  overlay = h('div', { class: 'callov', role: 'dialog', 'aria-label': '通话' });
  document.body.append(overlay);
  render();
}

function stateText() {
  const n = cur.peer ? cur.peer.display_name : '对方';
  return { calling: '正在呼叫…', ringing: cur.media === 'video' ? '邀请你视频通话' : '邀请你语音通话', connecting: '正在连接…', connected: fmtDur(Date.now() - (cur.t0 || Date.now())), reconnecting: '网络不稳，正在重连…' }[cur.state] || n;
}

function render() {
  if (!overlay || !cur) return;
  const c = cur;
  const video = c.media === 'video';
  const top = h('div', { class: 'ctop' }, avatar(c.peer || { id: 0, display_name: '?' }, 84), h('div', { class: 'cname' }, c.peer ? c.peer.display_name : '对方'), h('div', { class: 'cstate' }, stateText()));
  const controls = h('div', { class: 'cctl' });
  if (c.state === 'ringing') {
    controls.append(circle('call-end', '拒绝', 'red', decline), circle(video ? 'video' : 'phone', '接听', 'green', accept));
  } else {
    controls.append(circle(c.muted ? 'mic-off' : 'mic', c.muted ? '取消静音' : '静音', c.muted ? 'on' : '', toggleMute));
    if (video) controls.append(circle(c.camOff ? 'video-off' : 'video', c.camOff ? '打开摄像头' : '关闭摄像头', c.camOff ? 'on' : '', toggleCam));
    if (video && c.canFlip) controls.append(circle('flip', '翻转', '', flipCamera));
    if (video && document.pictureInPictureEnabled) controls.append(circle('pip', '浮窗', '', openPip));
    controls.append(circle('call-end', '挂断', 'red', hangup));
  }
  c.remoteEl = video ? h('video', { class: 'cremote', autoplay: true, playsinline: true }) : h('audio', { autoplay: true });
  c.localEl = video ? h('video', { class: 'clocal' + (c.facing === 'environment' ? ' back' : ''), autoplay: true, muted: true, playsinline: true }) : null;
  c.timeEl = top.querySelector('.cstate');
  overlay.className = 'callov' + (video && (c.state === 'connected' || c.state === 'reconnecting') ? ' vid' : '');
  overlay.replaceChildren(c.remoteEl, ...(c.localEl ? [c.localEl] : []), top, controls);
  if (c.local) attachLocal();
  if (c.remote) attachRemote();
}

const circle = (ic, label, tone, fn) => h('button', { type: 'button', class: 'cbtn ' + tone, 'aria-label': label, title: label, onclick: fn }, icon(ic, 26), h('span', null, label));

function attachLocal() {
  if (cur && cur.localEl && cur.local) {
    cur.localEl.srcObject = cur.local;
    cur.localEl.muted = true;
  }
}
function attachRemote() {
  if (cur && cur.remoteEl && cur.remote) {
    cur.remoteEl.srcObject = cur.remote;
    cur.remoteEl.play && cur.remoteEl.play().catch(() => {});
  }
}
function tick() {
  if (cur && cur.timeEl) cur.timeEl.textContent = stateText();
}
function toggleMute() {
  cur.muted = !cur.muted;
  cur.local.getAudioTracks().forEach((t) => (t.enabled = !cur.muted));
  render();
}
function toggleCam() {
  cur.camOff = !cur.camOff;
  cur.local.getVideoTracks().forEach((t) => (t.enabled = !cur.camOff));
  render();
}

// ------------------------------------------------------------------ 铃声（WebAudio 合成，无外部文件）
function startTone(pattern) {
  stopTones();
  try {
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const play = () => {
      const o = ac.createOscillator();
      const g = ac.createGain();
      o.type = 'sine';
      o.frequency.value = pattern.freq;
      g.gain.value = 0.0001;
      o.connect(g).connect(ac.destination);
      const t = ac.currentTime;
      g.gain.exponentialRampToValueAtTime(0.18, t + 0.05);
      g.gain.exponentialRampToValueAtTime(0.0001, t + pattern.len);
      o.start(t);
      o.stop(t + pattern.len + 0.05);
    };
    play();
    ringer = { ac, timer: setInterval(play, pattern.every) };
  } catch {}
}
const startRingtone = () => {
  startTone({ freq: 660, len: 0.9, every: 2200 });
  navigator.vibrate && navigator.vibrate([400, 200, 400]);
};
const startRingback = () => startTone({ freq: 440, len: 1.0, every: 3000 });
function stopTones() {
  if (!ringer) return;
  clearInterval(ringer.timer);
  try {
    ringer.ac.close();
  } catch {}
  ringer = null;
}

// ------------------------------------------------------------------ 翻转摄像头
async function detectFlip() {
  if (!cur || cur.media !== 'video' || !cur.local) return;
  try {
    const cams = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
    cur.canFlip = cams.length > 1;
    cur.facing = 'user';
    render();
  } catch {}
}

async function flipCamera() {
  if (!cur || !cur.local || cur.flipping) return;
  cur.flipping = true;
  const next = cur.facing === 'environment' ? 'user' : 'environment';
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: next }, width: { ideal: 960 }, height: { ideal: 540 } } });
    const nt = s.getVideoTracks()[0];
    nt.enabled = !cur.camOff;
    const old = cur.local.getVideoTracks()[0];
    const sender = cur.pc && cur.pc.getSenders().find((x) => x.track && x.track.kind === 'video');
    if (sender) await sender.replaceTrack(nt);
    old && (cur.local.removeTrack(old), old.stop());
    cur.local.addTrack(nt);
    cur.facing = next;
    render();
  } catch {
    toast('无法切换摄像头', 'err');
  }
  if (cur) cur.flipping = false;
}

// ------------------------------------------------------------------ 浮窗（画中画）
async function openPip() {
  const v = cur && cur.remoteEl;
  if (!v || !v.requestPictureInPicture) return toast('这个浏览器不支持浮窗');
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await v.requestPictureInPicture();
  } catch {
    toast('浮窗打不开：请在系统设置里给浏览器开启“画中画”', 'err');
  }
}

// Chrome 在视频通话里会在你切走应用时自动进入画中画（需要页面注册这个动作）
function armAutoPip() {
  if (!cur || cur.media !== 'video' || !('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.setActionHandler('enterpictureinpicture', () => {
      const v = cur && cur.remoteEl;
      v && v.requestPictureInPicture && v.requestPictureInPicture().catch(() => {});
    });
  } catch {}
}

// ------------------------------------------------------------------ 通话时保持屏幕常亮
async function grabWakeLock() {
  try {
    if (navigator.wakeLock && !wakeLock) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => (wakeLock = null));
    }
  } catch {}
}
function releaseWakeLock() {
  try {
    wakeLock && wakeLock.release();
  } catch {}
  wakeLock = null;
}
document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && cur && grabWakeLock());
