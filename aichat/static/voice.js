// 语音消息（参考 Telegram）：
// 录音：按住说话，松开发送；上滑锁定后可松手，左滑取消；轻点直接进入锁定模式。锁定后可暂停/继续、停止试听再发送。
// 播放：波形 + 拖动/点按跳转、倍速、记住播放位置、播完自动播下一条未听的语音。
import { h, icon, iconBtn, toast } from './util.js';

export const MAX_MS = 5 * 60 * 1000;
const MIN_MS = 700;
const BARS = 48;

export function voiceSupported() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder && window.isSecureContext);
}

function pickMime() {
  const cands = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'];
  return cands.find((m) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || '';
}

export const fmtDur = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

// 把采样到的音量（0-1）压缩成 n 根柱子（0-31）
function toBars(levels, n = BARS) {
  if (!levels.length) return [];
  const out = [];
  for (let i = 0; i < n; i++) {
    const a = Math.floor((i * levels.length) / n);
    const b = Math.max(a + 1, Math.floor(((i + 1) * levels.length) / n));
    let peak = 0;
    for (let k = a; k < b && k < levels.length; k++) peak = Math.max(peak, levels[k]);
    out.push(peak);
  }
  const max = Math.max(0.08, ...out);
  return out.map((v) => Math.round(Math.min(1, v / max) * 31));
}

// ------------------------------------------------------------------ 录音
// 返回控制器：{ stop(), cancel(), pause(), resume(), paused, elapsed(), levels }
export async function startRecording({ onTick, onLevel } = {}) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch (e) {
    toast(e && e.name === 'NotAllowedError' ? '没有麦克风权限，请在浏览器的站点设置里允许' : '无法使用麦克风', 'err');
    return null;
  }
  const mime = pickMime();
  let rec;
  try {
    rec = new MediaRecorder(stream, { ...(mime ? { mimeType: mime } : {}), audioBitsPerSecond: 48000 });
  } catch {
    rec = new MediaRecorder(stream);
  }
  const chunks = [];
  rec.ondataavailable = (e) => e.data && e.data.size && chunks.push(e.data);

  // 音量采样：用于录音时的实时波形和最终的波形数据
  const levels = [];
  let ac = null;
  let analyser = null;
  let buf = null;
  try {
    ac = new (window.AudioContext || window.webkitAudioContext)();
    const src = ac.createMediaStreamSource(stream);
    analyser = ac.createAnalyser();
    analyser.fftSize = 1024;
    buf = new Uint8Array(analyser.fftSize);
    src.connect(analyser);
  } catch {}
  const sample = () => {
    if (!analyser) return 0;
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    return Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
  };

  let acc = 0; // 已累计的录音时长（不含暂停）
  let since = performance.now();
  let paused = false;
  let done = false;
  const elapsed = () => acc + (paused ? 0 : performance.now() - since);
  const release = () => {
    stream.getTracks().forEach((t) => t.stop());
    try {
      ac && ac.close();
    } catch {}
  };
  const timer = setInterval(() => {
    const ms = elapsed();
    if (!paused) {
      const lv = sample();
      levels.push(lv);
      onLevel && onLevel(lv);
    }
    onTick && onTick(ms);
    if (ms >= MAX_MS) ctrl.onLimit && ctrl.onLimit();
  }, 80);
  rec.start(500);

  const ctrl = {
    onLimit: null,
    levels,
    get paused() {
      return paused;
    },
    elapsed,
    pause() {
      if (paused || done || rec.state !== 'recording' || !rec.pause) return false;
      rec.pause();
      acc += performance.now() - since;
      paused = true;
      return true;
    },
    resume() {
      if (!paused || done || !rec.resume) return false;
      rec.resume();
      since = performance.now();
      paused = false;
      return true;
    },
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
        const ms = elapsed();
        rec.onstop = () => {
          release();
          if (ms < MIN_MS || !chunks.length) return resolve({ tooShort: true });
          const type = (rec.mimeType || mime || 'audio/webm').split(';')[0];
          const ext = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
          resolve({ blob: new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' }), duration_ms: Math.round(ms), ext, waveform: toBars(levels) });
        };
        try {
          if (rec.state === 'paused' && rec.resume) rec.resume();
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

// ------------------------------------------------------------------ 波形
function fallbackBars(seed, n = BARS) {
  // 老消息没有波形数据：按 id 生成稳定的"伪波形"，比一根直线好看
  let x = (seed * 9301 + 49297) % 233280;
  const out = [];
  for (let i = 0; i < n; i++) {
    x = (x * 9301 + 49297) % 233280;
    out.push(6 + Math.round((x / 233280) * 18));
  }
  return out;
}

function waveEl(values) {
  const el = h('div', { class: 'wave', 'aria-hidden': 'true' });
  for (const v of values) {
    const b = h('i');
    b.style.height = `${Math.max(12, Math.round((v / 31) * 100))}%`;
    el.append(b);
  }
  return el;
}

function paintProgress(wave, frac) {
  const bars = wave.children;
  const n = bars.length;
  const upto = Math.round(frac * n);
  for (let i = 0; i < n; i++) bars[i].classList.toggle('on', i < upto);
}

// ------------------------------------------------------------------ 播放
const pos = (() => {
  // 记住播放到哪里（只在本机）；播完清除
  const KEY = 'voice-pos';
  let m = {};
  try {
    m = JSON.parse(localStorage.getItem(KEY) || '{}');
  } catch {}
  const save = () => {
    try {
      const keys = Object.keys(m);
      if (keys.length > 200) delete m[keys[0]];
      localStorage.setItem(KEY, JSON.stringify(m));
    } catch {}
  };
  return { get: (id) => m[id] || 0, set: (id, t) => ((m[id] = t), save()), clear: (id) => (delete m[id], save()) };
})();

const played = (() => {
  const KEY = 'voice-played';
  let s = new Set();
  try {
    s = new Set(JSON.parse(localStorage.getItem(KEY) || '[]'));
  } catch {}
  return {
    has: (id) => s.has(id),
    add(id) {
      s.add(id);
      try {
        localStorage.setItem(KEY, JSON.stringify([...s].slice(-500)));
      } catch {}
    },
  };
})();

let playing = null; // 当前播放器的 stop()
let rateNow = 1;
let rateSaver = null;
export function setVoiceRate(r, saver) {
  rateNow = r || 1;
  if (saver) rateSaver = saver;
}
let autoNext = true;
export function setAutoNext(v) {
  autoNext = !!v;
}
export function stopAllVoice() {
  playing && playing();
}

// opts: { mine, onPlayed }
const hiddenTranscripts = new Set();
export const transcriptHidden = (id) => hiddenTranscripts.has(id);
export function toggleTranscript(id) {
  hiddenTranscripts.has(id) ? hiddenTranscripts.delete(id) : hiddenTranscripts.add(id);
}

export function voicePlayer(a, opts = {}) {
  const total = a.duration_ms || 0;
  const values = a.waveform && a.waveform.length ? a.waveform : fallbackBars(a.id || 1);
  const wave = waveEl(values);
  const time = h('span', { class: 'vtime' }, fmtDur(total));
  const btn = h('button', { class: 'vplay', type: 'button', 'aria-label': '播放语音' }, icon('play', 16));
  const speed = h('button', { class: 'vspeed', type: 'button', title: '播放速度', 'aria-label': '播放速度', hidden: true }, `${rateNow}×`);
  const unplayed = !opts.mine && !played.has(a.id);
  const dot = h('span', { class: 'vdot', hidden: !unplayed, title: '未播放' });
  let audio = null;
  let raf = 0;
  const durMs = () => (audio && isFinite(audio.duration) && audio.duration > 0 ? audio.duration * 1000 : total);
  const render = () => {
    if (!audio) return;
    const frac = Math.min(1, (audio.currentTime * 1000) / (durMs() || 1));
    paintProgress(wave, frac);
    time.textContent = fmtDur(audio.currentTime * 1000);
  };
  const loop = () => {
    render();
    raf = requestAnimationFrame(loop);
  };
  const setIdle = () => {
    cancelAnimationFrame(raf);
    btn.replaceChildren(icon('play', 16));
    btn.setAttribute('aria-label', '播放语音');
    row.classList.remove('playing');
  };
  const stop = () => {
    if (audio) {
      audio.pause();
      if (audio.currentTime > 1 && audio.currentTime < audio.duration - 0.5) pos.set(a.id, audio.currentTime);
    }
    setIdle();
    if (playing === stop) playing = null;
  };
  const ensureAudio = () => {
    if (audio) return audio;
    audio = new Audio(a.url);
    audio.preload = 'auto';
    audio.addEventListener('ended', () => {
      pos.clear(a.id);
      setIdle();
      paintProgress(wave, 0);
      time.textContent = fmtDur(total);
      if (playing === stop) playing = null;
      if (autoNext) playNext(row);
    });
    audio.addEventListener('error', () => {
      toast('这条语音在当前浏览器里播放不了', 'err');
      stop();
    });
    return audio;
  };
  const play = async () => {
    if (playing && playing !== stop) playing();
    const au = ensureAudio();
    au.playbackRate = rateNow;
    const saved = pos.get(a.id);
    if (saved && au.currentTime < 0.1) {
      try {
        au.currentTime = saved;
      } catch {}
    }
    playing = stop;
    btn.replaceChildren(icon('pause', 16));
    btn.setAttribute('aria-label', '暂停');
    row.classList.add('playing');
    speed.hidden = false;
    if (!opts.mine && !played.has(a.id)) {
      played.add(a.id);
      dot.hidden = true;
      opts.onPlayed && opts.onPlayed();
    }
    try {
      await au.play();
      loop();
    } catch {
      stop();
    }
  };
  btn.addEventListener('click', () => (playing === stop ? stop() : play()));
  speed.addEventListener('click', () => {
    const rates = [1, 1.5, 2];
    rateNow = rates[(rates.indexOf(rateNow) + 1) % rates.length] || 1;
    speed.textContent = `${rateNow}×`;
    if (audio) audio.playbackRate = rateNow;
    rateSaver && rateSaver(rateNow);
  });
  // 点按/拖动波形跳转
  const seek = (e) => {
    const r = wave.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const au = ensureAudio();
    const go = () => {
      au.currentTime = (frac * durMs()) / 1000;
      render();
    };
    if (au.readyState >= 1) go();
    else au.addEventListener('loadedmetadata', go, { once: true });
    paintProgress(wave, frac);
    time.textContent = fmtDur(frac * durMs());
    if (playing !== stop) play();
  };
  wave.addEventListener('pointerdown', (e) => {
    e.stopPropagation();
    wave.setPointerCapture?.(e.pointerId);
    seek(e);
    const mv = (ev) => seek(ev);
    const up = () => {
      wave.removeEventListener('pointermove', mv);
      wave.removeEventListener('pointerup', up);
      wave.removeEventListener('pointercancel', up);
    };
    wave.addEventListener('pointermove', mv);
    wave.addEventListener('pointerup', up);
    wave.addEventListener('pointercancel', up);
  });
  const saved = pos.get(a.id);
  if (saved && total) paintProgress(wave, Math.min(1, (saved * 1000) / total));
  const row = h('div', { class: 'voice', dataset: { aid: a.id } }, btn, wave, h('div', { class: 'vside' }, time, speed, dot));
  row.__play = play;
  row.__unplayed = () => !opts.mine && !played.has(a.id);
  if (a.transcript) {
    if (transcriptHidden(a.id)) return row;
    return h('div', { class: 'voice-wrap' }, row, h('div', { class: 'transcript' }, h('small', { class: 'muted' }, '语音转写'), h('div', null, a.transcript)));
  }
  const busy = ['queued', 'running'].includes(a.transcription_status);
  if (busy || a.transcription_status === 'failed') {
    const errors = { asr_no_speech: '未识别到清晰语音', asr_too_long: '语音超过转写时长限制', asr_model_missing: '服务器尚未安装语音模型', asr_not_installed: '服务器尚未安装语音识别依赖', asr_disabled: '服务器已关闭转文字', asr_ffmpeg_missing: '服务器缺少 FFmpeg', asr_timeout: '转写超时' };
    const text = busy ? (a.transcription_status === 'queued' ? '等待转文字…' : '正在转文字…') : (errors[a.transcription_error] || '转写失败') + '，长按语音可重试';
    return h('div', { class: 'voice-wrap' }, row, h('div', { class: 'transcript asr-status muted', role: 'status', 'aria-live': 'polite' }, text));
  }
  return row;
}

// 播完一条后，自动播下面最近一条"别人发的、还没听过"的语音
function playNext(row) {
  const all = [...document.querySelectorAll('.msgs .voice')];
  const i = all.indexOf(row);
  if (i < 0) return;
  const next = all.slice(i + 1).find((el) => el.__unplayed && el.__unplayed());
  if (next) {
    next.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    setTimeout(() => next.__play && next.__play(), 250);
  }
}

// ------------------------------------------------------------------ 录音条
// 三种状态：hold（按住中）→ locked（锁定，可暂停）→ review（停止后试听）
export function recordingBar({ onCancel, onSend, onPauseToggle, onStop }) {
  const time = h('span', { class: 'rtime' }, '0:00');
  const live = h('div', { class: 'rlive', 'aria-hidden': 'true' });
  const hint = h('span', { class: 'rhint' }, '← 左滑取消');
  const lockHint = h('div', { class: 'rlock', 'aria-hidden': 'true' }, icon('lock', 16), h('span', null, '上滑锁定'));
  const pauseBtn = iconBtn('pause', '暂停录音', () => onPauseToggle && onPauseToggle(), 'rpause');
  const stopBtn = iconBtn('stop', '停止并试听', () => onStop && onStop(), 'rstop');
  const sendBtn = h('button', { class: 'send', type: 'button', 'aria-label': '发送语音', title: '发送语音', onclick: () => onSend() }, icon('send', 20));
  const cancelBtn = iconBtn('trash', '取消录音', onCancel, 'rcancel');
  const dot = h('span', { class: 'rdot' });
  const reviewSlot = h('div', { class: 'rreview', hidden: true });
  const el = h('div', { class: 'recbar hold' }, cancelBtn, dot, time, live, hint, pauseBtn, stopBtn, reviewSlot, sendBtn, lockHint);
  const MAXBARS = 36;
  return {
    el,
    setTime: (ms) => (time.textContent = fmtDur(ms)),
    level(v) {
      const b = h('i');
      b.style.height = `${Math.max(10, Math.round(v * 100))}%`;
      live.append(b);
      while (live.children.length > MAXBARS) live.firstChild.remove();
    },
    setMode(mode) {
      el.classList.remove('hold', 'locked', 'review');
      el.classList.add(mode);
      hint.textContent = mode === 'hold' ? '← 左滑取消' : '';
    },
    setPaused(p) {
      el.classList.toggle('paused', p);
      pauseBtn.replaceChildren(icon(p ? 'mic' : 'pause', 18));
      pauseBtn.title = p ? '继续录音' : '暂停录音';
      pauseBtn.setAttribute('aria-label', pauseBtn.title);
    },
    setDrag(dx, dy) {
      el.style.setProperty('--dx', `${Math.min(0, dx)}px`);
      el.classList.toggle('will-cancel', dx < -60);
      lockHint.classList.toggle('near', dy < -40);
    },
    showReview(player) {
      reviewSlot.replaceChildren(player);
      reviewSlot.hidden = false;
    },
  };
}
