// WebSocket：断线自动重连，重连后触发 resync（由各视图按序号补齐缺失的消息）
import { S, emit } from './store.js';

let ws = null;
let retry = 0;
let pingTimer = null;
let everOpened = false;
let stopped = false;

export function connect() {
  stopped = false;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    S.online = true;
    emit('online', true);
    clearInterval(pingTimer);
    pingTimer = setInterval(() => ws && ws.readyState === 1 && ws.send(JSON.stringify({ type: 'ping' })), 25000);
    if (everOpened) emit('resync');
    everOpened = true;
    retry = 0;
  };
  ws.onmessage = (ev) => {
    let d;
    try {
      d = JSON.parse(ev.data);
    } catch {
      return;
    }
    switch (d.type) {
      case 'hello':
        S.agent = d.agent;
        emit('agent', S.agent);
        break;
      case 'message.created':
        emit('msg:new', d.message);
        break;
      case 'message.updated':
      case 'message.deleted':
        emit('msg:update', d.message);
        break;
      case 'room.updated':
        emit('room:update', d.room);
        break;
      case 'room.removed':
        emit('room:removed', d.room_id);
        break;
      case 'read.updated':
        emit('read', d);
        break;
      case 'task.updated':
        emit('task', d.task);
        break;
      case 'agent.status':
        S.agent = d.status;
        emit('agent', S.agent);
        break;
      case 'call.signal':
        emit('call', d);
        break;
      case 'user.updated':
        emit('user:updated', d.user);
        break;
      case 'typing':
        emit('typing', d);
        break;
      case 'reaction.updated':
        emit('reaction', d);
        break;
      case 'session.revoked':
        stopped = true;
        location.reload();
        break;
      case 'memory.updated':
        emit('memory:update', d.room_id);
        break;
    }
  };
  ws.onclose = (ev) => {
    S.online = false;
    emit('online', false);
    clearInterval(pingTimer);
    if (stopped || ev.code === 4401) return;
    const wait = Math.min(1000 * 2 ** retry, 15000);
    retry += 1;
    setTimeout(connect, wait);
  };
  ws.onerror = () => {};
}

export function disconnect() {
  stopped = true;
  everOpened = false;
  clearInterval(pingTimer);
  if (ws) ws.close();
}

// 发送轻量控制消息（例如“正在输入”）；连接不可用时静默丢弃
export function wsSend(obj) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}
