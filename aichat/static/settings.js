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
  const tabs = [['account', '账号与通知'], ['agent', 'Agent 接入'], ...(S.me.role === 'owner' || S.me.can_add_users ? [['members', '成员']] : []), ['about', '关于']];
  const show = (t) => {
    nav.replaceChildren(...tabs.map(([k, l]) => h('button', { type: 'button', role: 'tab', class: k === t ? 'on' : '', 'aria-selected': String(k === t), onclick: () => show(k) }, l)));
    content.replaceChildren(h('div', { class: 'loading' }, '加载中…'));
    ({ account: renderAccount, agent: renderAgent, members: renderMembers, about: renderAbout })[t](content);
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
  let st, creds = [];
  try {
    st = (await get('/api/agent/status')).status;
    S.agent = st;
    emit('agent', st);
    if (S.me.role === 'owner') creds = (await get('/api/agent/credentials')).credentials;
  } catch (e) {
    el.replaceChildren(h('div', { class: 'empty' }, e.message));
    return;
  }
  const origin = location.origin;
  const active = creds.filter((c) => !c.revoked_at);
  const label = h('input', { type: 'text', placeholder: '例如：我的笔记本', maxlength: '60', 'aria-label': '凭证备注' });
  const stateText = { unconfigured: '未配置', offline: '离线', online: '在线' }[st.state];
  const tokenShown = lastToken || '<粘贴上面创建的令牌>';
  const cmd = `# 安装（在运行 Agent 的那台设备上）\npip install -e .   # 或：pip install aichat\n\nexport AICHAT_AGENT_TOKEN='${tokenShown}'\n\n# 演示（规则实现，不是真正的模型）\naichat-bridge --url ${origin} --adapter mock\n\n# 任意 OpenAI 兼容服务\nexport AICHAT_LLM_API_KEY=… AICHAT_LLM_MODEL=…\naichat-bridge --url ${origin} --adapter openai\n\n# 把提示词交给本地命令行 Agent（例如 Codex CLI）\naichat-bridge --url ${origin} --adapter command --command "codex exec -"`;
  const checks = h('div', { class: 'checks' });
  el.replaceChildren(
    h(
      'div',
      { class: 'card agent-card' },
      h('div', { class: 'ac-top' }, h('span', { class: 'dot ' + st.state }), h('b', null, 'Agent ' + stateText), st.adapter ? h('span', { class: 'tag' }, st.adapter) : null, S.me.role === 'owner' ? h('button', { class: 'btn small', type: 'button', style: { marginLeft: 'auto' }, onclick: () => pickAvatar('/api/agent/avatar') }, '更换 Agent 头像') : null),
      h('div', { class: 'ac-grid' }, kv('最近心跳', st.last_heartbeat ? fmtFull(st.last_heartbeat) : '—'), kv('最近回复', st.last_reply_at ? fmtFull(st.last_reply_at) : '—'), kv('最近整理', st.last_consolidation_at ? fmtFull(st.last_consolidation_at) : '—'), kv('积压', `${st.backlog_chat} 个回复 · ${st.backlog_events} 项待整理`), kv('规范版本', st.guide_version_agent ? `${st.guide_version_agent}${st.guide_version_agent === st.guide_version_current ? '' : '（已过期，当前 ' + st.guide_version_current + '）'}` : st.guide_version_current)),
      st.last_error ? h('div', { class: 'note warn' }, '最近错误：' + st.last_error.error) : null
    ),
    h('ol', { class: 'steps' },
      step('说明作用', 'Agent 负责两件事：被 @ 或在私聊里回复；以及在后台静默地把聊天整理成主题记忆。不接入也能正常聊天，AI 状态会显示“未配置”。'),
      step('选择运行位置', '运行在你当前的电脑，或一台你控制、持续在线的设备。设备关机时聊天照常保存，开机后 Agent 会补整理；互动请求超过有效期会标记“未及时处理”，不会补发过时的回答。'),
      step('选择 Agent 与模型', '内置适配器：mock（演示）、openai（任意 OpenAI 兼容服务）、anthropic、command（把提示词交给本地命令，如 Codex CLI）。平台数据协议不变，可随时更换。', h('p', { class: 'note' }, '提示：使用云端模型时，处理一条消息所需的当前会话内容会发送给你选择的服务商；不同会话的内容分别处理，不会混在一起。')),
      step(
        '创建凭证',
        S.me.role === 'owner' ? '凭证只交给 Bridge 控制层，可随时撤销；撤销后正在处理的任务立即失效。' : '只有 Owner 可以创建凭证。',
        S.me.role === 'owner'
          ? h('div', null,
              h('div', { class: 'inline' }, label, h('button', { class: 'btn primary', type: 'button', onclick: async () => {
                try {
                  const r = await post('/api/agent/credentials', { label: label.value });
                  lastToken = r.token;
                  renderAgent(el);
                } catch (e) {
                  toast(e.message, 'err');
                }
              } }, '创建凭证')),
              lastToken ? h('div', { class: 'token' }, h('div', null, '令牌只显示这一次，请立即保存：'), h('code', null, lastToken), h('button', { class: 'btn small', type: 'button', onclick: () => copyText(lastToken) }, '复制')) : null,
              active.length ? h('ul', { class: 'creds' }, active.map((c) => h('li', null, h('span', null, `#${c.id} ${c.label || '（无备注）'}`), h('small', null, fmtFull(c.created_at)), h('button', { class: 'link danger', type: 'button', onclick: async () => {
                if (!(await ask('撤销后，使用该凭证的 Bridge 将无法继续工作。', { ok: '撤销', danger: true, title: '撤销凭证' }))) return;
                await del(`/api/agent/credentials/${c.id}`);
                if (lastToken) lastToken = null;
                renderAgent(el);
              } }, '撤销')))) : null
            )
          : null
      ),
      step('交付规范', '指南与工具说明由服务器维护，是唯一可信版本；Bridge 会自动同步。', h('div', { class: 'links' }, h('a', { class: 'btn small', href: '/agent-guide.md', download: 'AGENT_GUIDE.md' }, icon('download', 14), ' 下载 AGENT_GUIDE.md'), h('a', { class: 'btn small', href: '/openapi.json', target: '_blank', rel: 'noopener' }, '接口说明（OpenAPI JSON）'))),
      step('启动 Bridge', '在运行 Agent 的设备上执行：', h('div', { class: 'code' }, h('pre', null, cmd), h('button', { class: 'btn small', type: 'button', onclick: () => copyText(cmd.replace(/^#.*\n/gm, '')) }, '复制'))),
      step('检查闭环', '验证连通性、任务领取与回复。', h('div', null, h('button', { class: 'btn', type: 'button', disabled: S.me.role !== 'owner' || null, onclick: async () => {
        checks.replaceChildren(h('small', null, '检查中…'));
        try {
          const r = await post('/api/agent/selfcheck');
          checks.replaceChildren(...r.checks.map((c) => h('div', { class: 'check-row ' + (c.ok ? 'ok' : 'bad') }, icon(c.ok ? 'check' : 'close', 14), c.name)), r.ok ? h('div', { class: 'note' }, '已在你与 Agent 的私聊里发出测试消息。', h('button', { class: 'link', type: 'button', onclick: () => { window.__closeSettings && window.__closeSettings(); emit('open-room', { id: r.room_id }); } }, '去看回复')) : h('div', { class: 'note warn' }, '还没有通过：请先创建凭证并启动 Bridge。'));
        } catch (e) {
          checks.replaceChildren(h('div', { class: 'note warn' }, e.message));
        }
      } }, '运行接入自检'), checks))
    )
  );
}

const kv = (k, v) => h('div', { class: 'kv' }, h('small', null, k), h('span', null, v));
const step = (title, text, extra) => h('li', null, h('div', { class: 'step-body' }, h('b', null, title), h('p', null, text), extra));

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
