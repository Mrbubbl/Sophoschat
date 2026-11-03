// 语音消息：录音（MediaRecorder）与播放。点一下开始，再点发送；可取消。
import { h, icon, iconBtn, toast } from './util.js';

export const MAX_MS = 5 * 60 * 1000;
const MIN_MS = 800;

export function voiceSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder && window.isSecureContext);
}

function pickMime() {
  const cands = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
  return cands.find((m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || '';
}

const fmtDur = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};
export { fmtDur };

// 开始录音；返回控制器 { stop(): Promise<{blob, duration_ms, ext}|null>, cancel(), elapsed() }
export async function startRecording(onTick) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    toast(e && e.name === 'NotAllowedError' ? '没有麦克风权限，请在浏览器的站点设置里允许' : '无法使用麦克风', 'err');
    return null;
  }
  const mime = pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  rec.ondataavailable = (e) => e.data && e.data.size && chunks.push(e.data);
  const t0 = performance.now();
  let done = false;
  const release = () => stream.getTracks().forEach((t) => t.stop());
  const timer = setInterval(() => {
    const ms = performance.now() - t0;
    onTick && onTick(ms);
    if (ms >= MAX_MS) ctrl.onLimit && ctrl.onLimit();
  }, 200);
  rec.start(500);
  const ctrl = {
    onLimit: null,
    elapsed: () => performance.now() - t0,
    cancel() {
      if (done) return;
      done = true;
      clearInterval(timer);
      try {
        rec.stop();
      } catch {}
      release();
    },
    stop() {
      return new Promise((resolve) => {
        if (done) return resolve(null);
        done = true;
        clearInterval(timer);
        const ms = performance.now() - t0;
        rec.onstop = () => {
          release();
          if (ms < MIN_MS || !chunks.length) return resolve({ tooShort: true });
          const type = (rec.mimeType || mime || 'audio/webm').split(';')[0];
          const ext = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
          resolve({ blob: new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' }), duration_ms: Math.round(ms), ext });
        };
        try {
          rec.stop();
        } catch {
          release();
          resolve(null);
        }
      });
    },
  };
  return ctrl;
}

// ---- 播放器（同一时间只播一条）
let playing = null;
export function voicePlayer(a) {
  const total = a.duration_ms || 0;
  const bar = h('i', { class: 'vbar' });
  const time = h('span', { class: 'vtime' }, fmtDur(total));
  const btn = h('button', { class: 'vplay', type: 'button', 'aria-label': '播放语音' }, icon('play', 16));
  let audio = null;
  const reset = () => {
    btn.replaceChildren(icon('play', 16));
    bar.style.width = '0%';
    time.textContent = fmtDur(total);
  };
  const stop = () => {
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    reset();
    if (playing === stop) playing = null;
  };
  btn.addEventListener('click', async () => {
    if (playing === stop) return stop();
    if (playing) playing();
    if (!audio) {
      audio = new Audio(a.url);
      audio.preload = 'auto';
      audio.addEventListener('timeupdate', () => {
        const dur = isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : total;
        bar.style.width = Math.min(100, (audio.currentTime * 1000 * 100) / (dur || 1)) + '%';
        time.textContent = fmtDur(audio.currentTime * 1000);
      });
      audio.addEventListener('ended', stop);
      audio.addEventListener('error', () => {
        toast('这条语音在当前浏览器里播放不了', 'err');
        stop();
      });
    }
    playing = stop;
    btn.replaceChildren(icon('pause', 16));
    try {
      await audio.play();
    } catch {
      stop();
    }
  });
  const row = h('div', { class: 'voice' }, btn, h('div', { class: 'vtrack' }, bar), time);
  if (!a.transcript) return row;
  return h('div', { class: 'voice-wrap' }, row, h('div', { class: 'transcript' }, a.transcript));
}

export function recordingBar({ onCancel, onSend }) {
  const time = h('span', { class: 'rtime' }, '0:00');
  const el = h('div', { class: 'recbar' }, iconBtn('trash', '取消录音', onCancel, 'rcancel'), h('span', { class: 'rdot' }), time, h('span', { class: 'rhint' }, '正在录音…'), h('button', { class: 'send', type: 'button', 'aria-label': '发送语音', title: '发送语音', onclick: onSend }, icon('send', 20)));
  return { el, setTime: (ms) => (time.textContent = fmtDur(ms)) };
}
