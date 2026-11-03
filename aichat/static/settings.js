// 设置：账号、Agent 接入引导（P0）、成员管理（Owner）
import { get, post, patch, del } from './api.js';
import { S, emit } from './store.js';
import { showTimes, setShowTimes, h, icon, iconBtn, avatar, modal, toast, ask, copyText, fmtFull, fmtRel, squareJpeg } from './util.js';
import { api } from './api.js';
import { loadUsers } from './rooms.js';
import { pushStatus, enablePush, disablePush } from './push.js';

let lastToken = null; // 令牌只在刚创建时可见，只保留在内存里

export function openSettings(tab = 'account') {
  const content = h('div', { class: 'set-content' });
  const nav = h('nav', { class: 'set-nav', role: 'tablist' });
  const tabs = [['account', '账号与通知'], ['agent', 'Agent 接入'], ...(S.me.role==='owner'?[['tools','工具与 MCP']]:[]), ...(S.me.role === 'owner' || S.me.can_add_users ? [['members', '成员']] : []), ['about', '关于']];
  const show = (t) => {
    nav.replaceChildren(...tabs.map(([k, l]) => h('button', { type: 'button', role: 'tab', class: k === t ? 'on' : '', 'aria-selected': String(k === t), onclick: () => show(k) }, l)));
    content.replaceChildren(h('div', { class: 'loading' }, '加载中…'));
    ({ account: renderAccount, agent: renderAgent, tools:renderTools, members: renderMembers, about: renderAbout })[t](content);
  };
  const close = modal({ title: '设置', wide: true, body: h('div', { class: 'settings' }, nav, content) });
  window.__closeSettings = close;
  show(tab);
}

// 选图 → 裁成正方形 → 上传；endpoint 是 /api/me/avatar 或（Owner 给 Agent 换头像）/api/agent/avatar
function pickAvatar(endpoint) {
  const inp = h('input', {
    type: 'file',
    accept: 'image/*',
    hidden: true,
    onchange: async () => {
      const f = inp.files[0];
      inp.remove();
      if (!f) return;
      try {
        const fd = new FormData();
        fd.append('file', await squareJpeg(f, 256), 'avatar.jpg');
        const r = await api('POST', endpoint, fd);
        emit('user:updated', r.user);
        toast('头像已更新');
      } catch (e) {
        toast(e.message, 'err');
      }
    },
  });
  document.body.append(inp);
  inp.click();
}

// ------------------------------------------------------------------ 账号
function renderAccount(el) {
  const old = h('input', { type: 'password', autocomplete: 'current-password', 'aria-label': '当前密码' });
  const nu = h('input', { type: 'password', autocomplete: 'new-password', 'aria-label': '新密码', placeholder: '至少 8 位' });
  const perm = typeof Notification === 'undefined' ? 'unsupported' : Notification.permission;
  const devices = h('div', { class: 'devices' }, h('div', { class: 'loading small' }, '加载中…'));
  loadDevices(devices);
  el.replaceChildren(
    h('div', { class: 'card' }, h('div', { class: 'who' }, h('button', { class: 'avatar-edit', type: 'button', title: '更换头像', 'aria-label': '更换头像', onclick: () => pickAvatar('/api/me/avatar') }, avatar(S.me, 56), h('span', { class: 'av-cam' }, icon('camera', 14))), h('div', null, h('b', null, S.me.display_name), h('div', { class: 'muted' }, '@' + S.me.username + (S.me.role === 'owner' ? ' · Owner' : '')), S.me.avatar_url ? h('button', { class: 'link', type: 'button', onclick: async () => { const r = await api('DELETE', '/api/me/avatar'); emit('user:updated', r.user); renderAccount(el); } }, '移除头像') : null)), h('p', { class: 'muted' }, '聊天内容保存在你自己部署的服务器上。')),
    h('h3', null, '修改密码'),
    h('div', { class: 'form' }, h('label', null, '当前密码', old), h('label', null, '新密码', nu), h('button', { class: 'btn', type: 'button', onclick: async () => {
      try {
        await post('/api/me/password', { old_password: old.value, new_password: nu.value });
        old.value = nu.value = '';
        toast('密码已更新，其他设备已下线');
      } catch (e) {
        toast(e.message, 'err');
      }
    } }, '更新密码')),
    h('h3', null, '聊天显示'),
    h('label', { class: 'switch-row' }, h('input', { type: 'checkbox', checked: showTimes(), onchange: (e) => { setShowTimes(e.target.checked); toast(e.target.checked ? '已显示每条消息的时间' : '已隐藏，只在间隔较久时显示时间（长按消息可看确切时间）'); } }), h('span', { class: 'sw-text' }, '每条消息显示发送时间', h('small', null, '关掉后更紧凑：消息间隔超过 5 分钟才会显示一次时间。只对这台设备生效。'))),
    h('h3', null, '通知'),
    notifySection(el, perm),
    h('h3', null, '登录设备'),
    devices,
    h('h3', null, '登录'),
    h('button', { class: 'btn danger-outline', type: 'button', onclick: async () => { await post('/api/logout'); location.reload(); } }, '退出登录')
  );
}

// 通知：本页提醒 + 离线推送（Web Push）
function notifySection(el, perm) {
  const box = h('div', { class: 'notify' });
  const redraw = () => renderAccount(el);
  const note = (t) => h('p', { class: 'muted small' }, t);
  if (perm === 'unsupported') {
    box.append(note('当前浏览器不支持通知。'));
  } else {
    if (perm === 'denied') box.append(note('通知已被浏览器屏蔽，请在浏览器的站点设置里允许。'));
    else if (perm !== 'granted') box.append(h('button', { class: 'btn', type: 'button', onclick: async () => { await Notification.requestPermission(); redraw(); } }, '开启页面内提醒'));
    else box.append(note('页面内提醒已开启：窗口不在前台，或收到其他会话的消息时提醒。'));
    const pushBox = h('div', { class: 'pushbox' }, h('div', { class: 'loading small' }, '检查离线推送…'));
    box.append(pushBox);
    pushStatus()
      .then((st) => {
        if (!st.supported) return pushBox.replaceChildren(note('这个浏览器不支持离线推送。iPhone 需要先把本站“添加到主屏幕”再打开。'));
        if (!st.serverAvailable) return pushBox.replaceChildren(note('服务器没有启用离线推送。'));
        pushBox.replaceChildren(
          h('label', { class: 'switch-row' }, h('input', { type: 'checkbox', checked: st.subscribed, onchange: async (e) => {
            e.target.disabled = true;
            try {
              if (e.target.checked) { await enablePush(); toast('离线推送已开启'); }
              else { await disablePush(); toast('离线推送已关闭'); }
            } catch (err) {
              toast(err.message, 'err');
            }
            redraw();
          } }), h('span', { class: 'sw-text' }, '离线推送（这台设备）', h('small', null, '关闭页面、手机锁屏后，新消息也会弹出通知。安卓需要能访问谷歌推送服务。')))
        );
      })
      .catch(() => pushBox.replaceChildren(note('无法获取离线推送状态。')));
  }
  return box;
}

// 登录设备：查看并退出
async function loadDevices(box) {
  try {
    const { sessions } = await get('/api/sessions');
    const others = sessions.filter((x) => !x.current);
    box.replaceChildren(
      ...sessions.map((x) =>
        h('div', { class: 'device' + (x.current ? ' cur' : '') }, icon('device', 18), h('div', { class: 'dinfo' }, h('b', null, x.device, x.current ? h('span', { class: 'tag' }, '当前设备') : null), h('small', null, `最近活动 ${fmtRel(x.last_seen_at)}${x.ip ? ' · ' + x.ip : ''}`)), x.current ? null : h('button', { class: 'btn small', type: 'button', onclick: async () => {
          await del('/api/sessions/' + x.id);
          toast('已退出该设备');
          loadDevices(box);
        } }, '退出'))
      ),
      others.length ? h('button', { class: 'btn danger-outline small', type: 'button', onclick: async () => {
        if (!(await ask('退出其他所有设备？这些设备需要重新输入密码才能登录。', { danger: true, ok: '退出' }))) return;
        await post('/api/sessions/revoke-others');
        toast('已退出其他设备');
        loadDevices(box);
      } }, '退出其他所有设备') : null
    );
  } catch (e) {
    box.replaceChildren(h('p', { class: 'muted' }, e.message));
  }
}

// ------------------------------------------------------------------ Agent 接入
async function renderAgent(el) {
  try {
    const st = (await get('/api/agent/status')).status;
    S.agent = st;
    emit('agent', st);
    const title = { unconfigured: '未配置', offline: '离线', online: '在线', paused: '已暂停', degraded: '模型调用异常' }[st.state] || st.state;
    const body = h('div', { class: 'agent-settings' }, h('h3', null, '服务端 Agent · ' + title),
      h('p', { class: 'note' }, 'AI 由服务器统一运行。普通成员只需聊天，无需安装终端 Agent、复制凭证或保持电脑在线。'),
      h('div', { class: 'ac-grid' }, kv('最近心跳', st.last_heartbeat ? fmtFull(st.last_heartbeat) : '—'), kv('指南版本', st.guide_version_current)),
      st.last_error ? h('p', { class: 'note warn' }, '最近错误：' + st.last_error.error) : null);
    el.replaceChildren(body);
    if (S.me.role === 'owner') {
      const { settings: cfg, sdk } = await get('/api/agent/settings');
      body.append(h('p', { class: 'muted small' }, `执行引擎：Claude Agent SDK ${sdk.version || '未安装'} · 要求 ${sdk.expected}`));
      const input = (name, label, type = 'text') => {
        const field = h('input', { type, value: cfg[name], 'aria-label': label, step: type === 'number' ? 'any' : undefined });
        return [h('label', { class: 'agent-field' }, h('span', null, label), field), field];
      };
      const [modelRow, model] = input('model', '默认 Claude 模型 ID');
      const key = h('input', { type: 'password', autocomplete: 'new-password', placeholder: cfg.configured ? '已保存，留空保持' : '填写服务端 API Key', 'aria-label': '服务端 API Key' });
      const [urlRow, url] = input('base_url', '兼容网关地址（可选）');
      const enabled = h('input', { type: 'checkbox', checked: cfg.enabled });
      const memory = h('input', { type: 'checkbox', checked: cfg.memory_enabled });
      const form = h('div', { class: 'agent-form' }, modelRow, h('label', { class: 'agent-field' }, h('span', null, '服务端 API Key'), key), urlRow,
        h('label', null, enabled, ' 启用服务端 AI'), h('label', null, memory, ' 启用后台记忆整理'));
      const numeric = {};
      for (const [name, label] of [['max_concurrency','总并发'],['reserved_chat_slots','交互预留名额'],['max_turns','单次最大轮数'],['timeout_seconds','单次超时（秒）'],['max_run_usd','单次费用上限（美元，SDK 估算）'],['monthly_usd','平台月额度（美元）'],['user_monthly_usd','成员月额度（美元）'],['room_monthly_usd','会话月额度（美元）']]) {
        const [row, field] = input(name, label, 'number'); numeric[name] = field; form.append(row);
      }
      const models = {};
      const advanced = h('details', null, h('summary', null, '按用途选择 Claude 模型（可选）'));
      for (const name of ['chat','delivery','memory']) {
        const field = h('input', { value: cfg.models[name] || '', placeholder: '沿用默认模型', 'aria-label': name + ' 模型' });
        models[name] = field; advanced.append(h('label', { class: 'agent-field' }, name, field));
      }
      form.append(advanced);
      form.append(h('button', { class: 'btn primary', type: 'button', onclick: async () => {
        try {
          const values = { model: model.value.trim(), base_url: url.value.trim(), enabled: enabled.checked, memory_enabled: memory.checked,
            ...Object.fromEntries(Object.entries(numeric).map(([k,v]) => [k, Number(v.value)])),
            models: Object.fromEntries(Object.entries(models).filter(([k,v]) => v.value.trim()).map(([k,v]) => [k, v.value.trim()])) };
          if (key.value) values.api_key = key.value;
          await api('PUT', '/api/agent/settings', values);
          key.value = '';
          toast('已保存；正在执行的旧配置任务已停止，新任务使用新设置');
          await renderAgent(el);
        } catch(e) { toast(e.message, 'err'); }
      } }, '保存服务端配置'));
      body.append(form);
      body.append(h('p', { class: 'note' }, '总并发为 1 时，交互预留设为 0，长任务期间其他 AI 请求会排队。月额度按已知费用与未知费用预留量共同控制。'));
      const checkBox = h('div', { class: 'checks', role: 'status' });
      const renderChecks = (r) => {
        checkBox.replaceChildren(...r.checks.map(c => h('div', { class: 'check-row ' + (c.ok ? 'ok' : 'bad') }, c.ok ? '✓ ' : '… ', c.name)),
          h('p', null, r.ok ? '自检通过：真实 SDK、作用域读取和文件持久化已验证。' : r.state === 'pending' ? '自检执行中，尚未通过…' : '自检未通过' + (r.error ? '：' + r.error : '')));
        if (r.state === 'pending' && r.task_id) setTimeout(async () => { if (!checkBox.isConnected) return; try { renderChecks(await get(`/api/agent/selfcheck/${r.task_id}`)); } catch(e) { checkBox.textContent = e.message; } }, 1500);
      };
      body.append(h('button', { class: 'btn', type: 'button', onclick: async () => { try { renderChecks(await post('/api/agent/selfcheck')); } catch(e) { toast(e.message, 'err'); } } }, '运行真实 SDK 与文件自检'), checkBox);
    }
    const usage = await get('/api/agent/usage');
    body.append(h('h3', null, '本月任务用量'), h('p', null, `已知费用 $${usage.known_cost_usd.toFixed(4)} · ${usage.unknown_attempts} 次费用未知（保留预算）`),
      h('div', { class: 'usage-list' }, usage.attempts.slice(0,20).map(a => h('div', { class: 'kv' }, `任务 #${a.task_id} / 第 ${a.attempt} 次 · ${a.profile}`, a.cost_usd === null ? '费用未知' : '$' + a.cost_usd.toFixed(4)))));
    body.append(h('a', { class: 'btn small', href: '/agent-guide.md', download: 'AGENT_GUIDE.md' }, '下载当前 Agent 指南'));
  } catch(e) { el.replaceChildren(h('p', { class: 'note warn' }, e.message)); }
}

const kv = (k, v) => h('div', { class: 'kv' }, h('small', null, k), h('span', null, v));
const step = (title, text, extra) => h('li', null, h('div', { class: 'step-body' }, h('b', null, title), h('p', null, text), extra));

async function renderTools(el) {
  try {
    const data=await get('/api/agent/integrations');const cfg=data.integrations;
    const enabled=h('input',{type:'checkbox',checked:data.registration_enabled});
    const provider=h('select',{'aria-label':'联网搜索服务'},[['auto','自动（无需 Key）'],['bing','Bing RSS'],['duckduckgo','DuckDuckGo'],['brave','Brave Search'],['tavily','Tavily'],['searxng','SearXNG']].map(([v,t])=>h('option',{value:v,selected:cfg.search.provider===v},t)));
    const url=h('input',{type:'text',value:cfg.search.url,placeholder:'可选服务地址；SearXNG 必填','aria-label':'搜索服务地址'});
    const key=h('input',{type:'password',autocomplete:'new-password',placeholder:cfg.search.api_key?'已保存，留空保持':'可选搜索 API Key','aria-label':'搜索 API Key'});
    const servers=h('textarea',{class:'mcp-config','aria-label':'MCP 服务配置',rows:16,spellcheck:false,value:JSON.stringify(cfg.mcp_servers,null,2)});
    const checks=h('div',{class:'mcp-checks'});
    Object.keys(cfg.mcp_servers).forEach(name=>{
      const result=h('span',{role:'status'});
      checks.append(h('div',{class:'kv'},h('button',{type:'button',class:'btn small',onclick:async()=>{
        result.textContent='连接中…';try {const r=await post('/api/agent/integrations/check',{name});result.textContent=r.status==='connected'?`已连接 · ${r.tool_count} 个工具：${r.tools.join('、')}`:`连接失败：${r.error}`;}catch(e){result.textContent=e.message;}
      }},'测试 '+name),result));
    });
    el.replaceChildren(
      h('h3',null,'Agent 完整执行环境'),
      h('p',{class:'note'},'所有 Agent 启用全部 Claude Code 工具并自动执行。支持 Bash、Python、npm、依赖安装、项目测试、构建和文件读写，工作目录在服务器持久保存。'),
      h('p',{class:'muted small'},`执行账号 UID ${data.execution.os_uid ?? '—'} · Python ${data.execution.python} · Node ${data.execution.node || '未安装'} · npm ${data.execution.npm || '未安装'}`),
      h('h3',null,'账号注册'),h('label',null,enabled,' 开放注册（新账号可直接登录和使用 Agent）'),
      h('button',{type:'button',class:'btn small',onclick:async()=>{try{await api('PUT','/api/registration',{enabled:enabled.checked});toast('注册设置已保存');}catch(e){toast(e.message,'err');}}},'保存注册设置'),
      h('h3',null,'联网搜索与网页浏览'),
      h('p',{class:'muted'},'默认搜索无需额外 Key。遇到搜索服务限流时可切换服务；动态网页支持打开、点击、填写、JavaScript 和截图。'),
      h('div',{class:'form'},h('label',null,'搜索服务',provider),h('label',null,'服务地址',url),h('label',null,'服务 Key',key)),
      h('h3',null,'外部 MCP 服务'),
      h('p',{class:'muted'},'支持 http、sse 和 stdio。接入日历、云盘等服务后，Agent 自动使用其工具；服务要求认证时，请配置 headers 或 env。'),
      h('details',null,h('summary',null,'配置示例'),h('pre',null,JSON.stringify({calendar:{type:'http',url:'https://your-calendar-service.example/mcp',headers:{Authorization:'Bearer YOUR_TOKEN'}},local:{type:'stdio',command:'npx',args:['-y','your-mcp-server'],env:{API_KEY:'YOUR_TOKEN'}}},null,2))),
      servers,h('p',{class:'muted small'},'已保存的 headers、env、args 用 __UNCHANGED__ 标记；保留标记即可沿用，不会显示明文凭证。删除一个服务项可断开连接。'),
      h('button',{type:'button',class:'btn primary',onclick:async()=>{try{
        await api('PUT','/api/agent/integrations',{mcp_servers:JSON.parse(servers.value),search:{provider:provider.value,url:url.value.trim(),api_key:key.value||cfg.search.api_key||''}});
        key.value='';toast('工具配置已保存，新任务立即使用');await renderTools(el);
      }catch(e){toast(e.message,'err');}}},'保存工具与 MCP 配置'),
      checks);
  } catch(e){el.replaceChildren(h('p',{class:'note warn'},e.message));}
}

// ------------------------------------------------------------------ 成员
async function renderMembers(el) {
  await loadUsers();
  const u = h('input', { type: 'text', placeholder: '用户名（字母数字）', autocomplete: 'off', 'aria-label': '用户名' });
  const d = h('input', { type: 'text', placeholder: '显示名', 'aria-label': '显示名' });
  const p = h('input', { type: 'password', placeholder: '初始密码（至少 8 位）', autocomplete: 'new-password', 'aria-label': '初始密码' });
  el.replaceChildren(
    h('h3', null, '成员'),
    h('div', { class: 'members big' }, [S.me, ...S.users].map((m) => h('div', { class: 'member' }, avatar(m, 34), h('div', null, h('b', null, m.display_name), h('div', { class: 'muted' }, '@' + m.username + (m.role === 'owner' ? ' · Owner' : m.can_add_users ? ' · 可添加成员' : ''))),
      S.me.role === 'owner' && m.role === 'user' ? h('label', { class: 'perm', title: '允许这位成员添加新账号' }, h('input', { type: 'checkbox', checked: !!m.can_add_users, onchange: async (e) => {
        try {
          const r = await api('PATCH', `/api/users/${m.id}/permissions`, { can_add_users: e.target.checked });
          emit('user:updated', r.user);
          toast(e.target.checked ? `${m.display_name} 现在可以添加成员` : `已收回 ${m.display_name} 添加成员的权限`);
          renderMembers(el);
        } catch (err) {
          toast(err.message, 'err');
          e.target.checked = !e.target.checked;
        }
      } }), h('span', null, '可添加成员')) : null))),
    h('h3', null, '添加成员'),
    h('div', { class: 'form' }, u, d, p, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
      try {
        await post('/api/users', { username: u.value, display_name: d.value || null, password: p.value });
        toast('已添加，并自动建立了与 Agent 及现有成员的私聊');
        emit('rooms:reload');
        renderMembers(el);
      } catch (e) {
        toast(e.message, 'err');
      }
    } }, '添加')),
    h('p', { class: 'muted small' }, '新成员只能看到自己所在的会话。把初始密码私下告诉对方，对方登录后可以自行修改。')
  );
}

function renderAbout(el) {
  const st = S.agent || {};
  el.replaceChildren(
    h('div', { class: 'card' }, h('b', null, 'SophosChat'), h('p', { class: 'muted' }, `平台版本 ${st.platform_version || '—'} · Agent 规范 ${st.guide_version_current || '—'}`)),
    h('h3', null, '数据与隔离'),
    h('ul', { class: 'plain' },
      h('li', null, '原始消息独立保存；Agent 的整理不会替代原文，可随时导出与备份。'),
      h('li', null, '不同会话的消息、回顾、主题记忆互不交换；同一个人参加多个会话也不会自动打通。'),
      h('li', null, 'Agent 的任务令牌只对一个会话有效，撤销凭证立即生效。'),
      h('li', null, '跨会话授权披露（P1）当前未启用。')
    ),
    h('h3', null, '键盘'),
    h('ul', { class: 'plain' }, h('li', null, 'Ctrl/⌘ + K：搜索全部聊天'), h('li', null, 'Enter 发送，Shift + Enter 换行'), h('li', null, '输入 @ 选择成员或 Agent'))
  );
}
