// 右侧面板：主题记忆（可纠正）、时间段回顾、收藏、会话设置与导出
import { get, post, patch, del, qs } from './api.js';
import { S, emit, on, room, roomTitle, userById, agentUser } from './store.js';
import { h, icon, iconBtn, avatar, fmtFull, fmtTime, fmtDay, toast, ask, modal, toggle } from './util.js';
import { resultItem } from './chatview.js';

const els = {};
const openTopics = new Set();
let seq = 0; // 用于丢弃过期的异步渲染

const TABS = [
  ['memory', '主题记忆'],
  ['summaries', '回顾'],
  ['stars', '收藏'],
  ['room', '会话设置'],
];
const STATE = { current: '', disputed: '有分歧', unverified: '待核实', superseded: '已替代', retired: '已过时' };
const ADOPT = { none: '未表态', adopted: '已采纳', rejected: '已拒绝', partial: '部分采纳' };
const NATURE = { stated: '说过', agent_suggestion: 'AI 建议', reported_fact: '转述的事实', speculation: '推测', conclusion: '共同结论', verified_fact: '已核实' };

export function initPanel(container) {
  els.tabs = h('div', { class: 'ptabs', role: 'tablist' });
  els.body = h('div', { class: 'pbody' });
  container.append(h('header', { class: 'panel-head' }, iconBtn('back', '返回聊天', () => emit('toggle-panel'), 'only-narrow'), h('h2', null, '回顾与记忆'), h('div', { class: 'grow' }), iconBtn('close', '收起面板', () => emit('toggle-panel'), 'only-wide')), els.tabs, els.body);
  on('room:opened', () => refresh());
  on('memory:update', (id) => id === S.cur && ['memory', 'summaries'].includes(S.panelTab) && refresh(true));
  on('stars:changed', () => S.panelTab === 'stars' && refresh());
  on('room:update', (r) => r.id === S.cur && S.panelTab === 'room' && refresh());
  on('agent', () => S.panelTab === 'memory' && S.cur && refresh(true));
  on('task', (t) => t.kind === 'consolidate' && t.room_id === S.cur && S.panelTab === 'memory' && refresh(true));
}

function setTab(t) {
  S.panelTab = t;
  refresh();
}

export function refresh(keepScroll) {
  els.tabs.replaceChildren(...TABS.map(([k, label]) => h('button', { class: 'ptab' + (S.panelTab === k ? ' on' : ''), role: 'tab', 'aria-selected': String(S.panelTab === k), onclick: () => setTab(k) }, label)));
  if (!S.cur || !room(S.cur)) {
    els.body.replaceChildren(h('div', { class: 'empty' }, '先选择一个聊天'));
    return;
  }
  const top = els.body.scrollTop;
  const done = () => keepScroll && (els.body.scrollTop = top);
  const f = { memory: renderMemory, summaries: renderSummaries, stars: renderStars, room: renderRoom }[S.panelTab];
  Promise.resolve(f()).then(done);
}

const guard = () => {
  const t = ++seq;
  return () => t === seq && S.cur && room(S.cur);
};

// ------------------------------------------------------------------ 主题记忆
async function renderMemory() {
  const id = S.cur;
  const ok = guard();
  if (!els.body.children.length) els.body.replaceChildren(h('div', { class: 'loading' }, '加载中…'));
  let t, c;
  try {
    [t, c] = await Promise.all([get(`/api/rooms/${id}/topics`), get(`/api/rooms/${id}/consolidation`)]);
  } catch (e) {
    els.body.replaceChildren(h('div', { class: 'empty' }, e.message));
    return;
  }
  if (!ok()) return;
  const r = room(id);
  const box = h('div', { class: 'mem' }, statusBlock(r, c));
  if (!t.topics.length && !t.pending_items.length) {
    box.append(
      h(
        'div',
        { class: 'empty big' },
        h('div', { class: 'empty-art' }, icon('history', 28)),
        h('p', null, '还没有主题记忆'),
        h('small', null, r.agent_in_room ? '正常聊天即可。Agent 会在后台把反复聊到的事项整理成稳定主题，并保留出处。' : '邀请 Agent 加入这个聊天后，它才会整理这里的内容。')
      )
    );
  }
  for (const tp of t.topics) box.append(topicCard(tp, t.topics));
  if (t.pending_items.length) {
    box.append(h('section', { class: 'topic pending' }, h('div', { class: 'topic-head static' }, h('b', null, '待归类'), h('small', null, `${t.pending_items.length} 条，Agent 暂时拿不准该归到哪个主题`)), h('div', { class: 'items' }, t.pending_items.map((it) => itemEl(it, t.topics)))));
  }
  els.body.replaceChildren(box);
}

function statusBlock(r, c) {
  const wrap = h('div', { class: 'mem-status' });
  if (!r.agent_in_room) {
    wrap.append(h('div', { class: 'note' }, 'Agent 不在这个会话中，不会读取或整理这里的内容。'));
    if (r.type !== 'agent_dm') wrap.append(h('button', { class: 'btn', type: 'button', onclick: () => setAgent(true) }, '邀请 Agent'));
    return wrap;
  }
  if (!r.consolidation_enabled) {
    wrap.append(h('div', { class: 'note' }, '自动整理已关闭，只保存原始聊天。'), h('button', { class: 'btn', type: 'button', onclick: () => setConsolidation(true) }, '开启自动整理'));
    return wrap;
  }
  const st = S.agent;
  const active = c.active;
  const line = active ? (active.status === 'leased' ? 'Agent 正在整理…' : '已排队，等待 Agent 领取') : c.pending_events ? `有 ${c.pending_events} 项新的聊天变更等待整理（讨论静默后自动开始）` : '已是最新';
  wrap.append(
    h('div', { class: 'ms-line' }, active ? h('span', { class: 'spin' }) : h('span', { class: 'dot ' + (st ? st.state : 'offline') }), h('span', null, line)),
    h('div', { class: 'ms-sub' }, st && st.last_consolidation_at ? '上次整理：' + fmtFull(st.last_consolidation_at) : '尚未整理过', st && st.state !== 'online' ? ` · Agent ${st.state === 'unconfigured' ? '未配置' : '离线'}，上线后补整理` : ''),
    h(
      'div',
      { class: 'ms-actions' },
      h('button', { class: 'btn small', type: 'button', disabled: active || !c.pending_events || null, onclick: consolidateNow }, '立即整理'),
      h('button', { class: 'btn small', type: 'button', onclick: reprocessDialog }, '重新整理…')
    )
  );
  return wrap;
}

async function consolidateNow() {
  try {
    await post(`/api/rooms/${S.cur}/consolidate`, { mode: 'pending' });
    toast('已请求整理');
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function reprocessDialog() {
  const r = room(S.cur);
  const from = h('input', { type: 'number', min: '1', value: '1', 'aria-label': '起始序号' });
  const to = h('input', { type: 'number', min: '1', value: String(r.max_seq), 'aria-label': '结束序号' });
  const close = modal({
    title: '重新整理指定范围',
    body: h('div', { class: 'form' }, h('p', null, '让 Agent 重新阅读这个范围内的原文，按当前内容校对已有主题与条目。人工纠正过的条目不会被改写。'), h('label', null, '从第几条消息', from), h('label', null, '到第几条消息', to)),
    actions: [
      h('button', { class: 'btn', type: 'button', onclick: () => close() }, '取消'),
      h(
        'button',
        {
          class: 'btn primary',
          type: 'button',
          onclick: async () => {
            try {
              await post(`/api/rooms/${S.cur}/consolidate`, { mode: 'reprocess', from_seq: +from.value, to_seq: +to.value });
              close();
              toast('已请求重新整理');
              refresh(true);
            } catch (e) {
              toast(e.message, 'err');
            }
          },
        },
        '开始'
      ),
    ],
  });
}

function topicCard(tp, allTopics) {
  const open = openTopics.has(tp.id);
  const head = h(
    'button',
    {
      class: 'topic-head',
      type: 'button',
      'aria-expanded': String(open),
      onclick: () => {
        open ? openTopics.delete(tp.id) : openTopics.add(tp.id);
        refresh(true);
      },
    },
    h('span', { class: 'chev' + (open ? ' open' : '') }, icon('down', 16)),
    h('b', null, tp.title),
    h('small', null, `${tp.item_count} 条`),
    tp.overview_stale || tp.stale_count ? h('span', { class: 'tag warn', title: '原文被编辑过，Agent 会在下次整理时重新核对' }, '待核对') : null
  );
  const card = h('section', { class: 'topic' }, head);
  if (tp.overview) card.append(h('p', { class: 'overview' }, tp.overview));
  if (open) {
    const detail = h('div', { class: 'items' }, h('div', { class: 'loading small' }, '加载中…'));
    card.append(detail);
    get(`/api/topics/${tp.id}`).then(({ topic }) => {
      detail.replaceChildren(
        ...(topic.items.length ? topic.items.map((it) => itemEl(it, allTopics)) : [h('div', { class: 'empty small' }, '这个主题下还没有条目')]),
        h(
          'div',
          { class: 'topic-actions' },
          h('button', { class: 'btn small', type: 'button', onclick: () => addItem(tp) }, '＋ 添加记忆'),
          h('button', { class: 'btn small', type: 'button', onclick: () => renameTopic(tp) }, '重命名'),
          allTopics.length > 1 ? h('button', { class: 'btn small', type: 'button', onclick: () => mergeTopic(tp, allTopics) }, '合并到…') : null,
          h('button', { class: 'btn small', type: 'button', onclick: () => archiveTopic(tp) }, '归档')
        )
      );
    }).catch((e) => detail.replaceChildren(h('div', { class: 'empty small' }, e.message)));
  }
  return card;
}

function itemEl(it, allTopics) {
  const author = it.author_user_id ? userById(it.author_user_id).display_name : '共同';
  const el = h('article', { class: 'item' + (it.state === 'superseded' || it.state === 'retired' ? ' dim' : '') + (it.author_kind === 'agent' ? ' by-agent' : '') });
  el.append(h('div', { class: 'content' }, it.content));
  const tags = h('div', { class: 'tags' }, h('span', { class: 'tag who' + (it.author_kind === 'agent' ? ' agent' : '') }, it.author_kind === 'agent' ? (author.toLowerCase() === 'agent' ? 'Agent' : 'Agent · ' + author) : author), h('span', { class: 'tag' }, NATURE[it.nature] || it.nature));
  if (STATE[it.state]) tags.append(h('span', { class: 'tag ' + (it.state === 'disputed' ? 'warn' : '') }, STATE[it.state]));
  if (it.adoption && it.author_kind === 'agent') tags.append(h('span', { class: 'tag adopt ' + it.adoption }, ADOPT[it.adoption]));
  if (it.source_stale) tags.append(h('span', { class: 'tag warn', title: '依赖的原文被编辑过，待 Agent 重新核对' }, '原文已编辑·待核对'));
  if (it.manual) tags.append(h('span', { class: 'tag manual', title: '人工纠正过，Agent 不会改写' }, '人工'));
  el.append(tags);
  const srcs = h('div', { class: 'srcs' });
  if (!it.sources.length) srcs.append(h('small', null, it.manual_origin ? '手工添加' : '来源已清理'));
  for (const s of it.sources) srcs.append(h('button', { class: 'src', type: 'button', title: '跳到原文', onclick: () => emit('jump', { id: s.message_id }) }, '原文 #' + s.message_id));
  el.append(srcs);
  const hist = h('div', { class: 'hist', hidden: true });
  const actions = h(
    'div',
    { class: 'actions' },
    h('button', { class: 'link', type: 'button', onclick: () => startEditItem(it, el) }, '编辑'),
    it.author_kind === 'agent' ? adoptionSelect(it) : null,
    it.state === 'retired' || it.state === 'superseded' ? h('button', { class: 'link', type: 'button', onclick: () => itemPatch(it, { state: 'current' }) }, '恢复') : h('button', { class: 'link', type: 'button', onclick: () => itemPatch(it, { state: 'retired' }) }, '标记过时'),
    it.topic_id === null && allTopics.length ? moveSelect(it, allTopics) : null,
    h('button', { class: 'link', type: 'button', onclick: () => toggleHistory(it, hist) }, '变化'),
    h('button', { class: 'link danger', type: 'button', onclick: () => deleteItem(it) }, '删除')
  );
  el.append(actions, hist);
  return el;
}

function adoptionSelect(it) {
  const sel = h('select', { 'aria-label': '采纳状态', class: 'mini' }, Object.entries(ADOPT).map(([k, v]) => h('option', { value: k, selected: it.adoption === k || null }, v)));
  sel.value = it.adoption || 'none';
  sel.addEventListener('change', () => itemPatch(it, { adoption: sel.value, reason: '用户手动设置采纳状态' }));
  return sel;
}

function moveSelect(it, topics) {
  const sel = h('select', { 'aria-label': '移到主题', class: 'mini' }, h('option', { value: '' }, '移到主题…'), topics.map((t) => h('option', { value: t.id }, t.title)));
  sel.addEventListener('change', () => sel.value && itemPatch(it, { topic_id: +sel.value }));
  return sel;
}

async function itemPatch(it, changes) {
  try {
    await patch(`/api/memory/items/${it.id}`, changes);
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function startEditItem(it, el) {
  const content = el.querySelector('.content');
  const ta = h('textarea', { class: 'edit-box', rows: '3', 'aria-label': '编辑记忆' });
  ta.value = it.content;
  const cancel = () => refresh(true);
  const save = async () => {
    const v = ta.value.trim();
    if (!v || v === it.content) return cancel();
    await itemPatch(it, { content: v, reason: '用户手动纠正' });
  };
  content.replaceChildren(ta, h('div', { class: 'edit-actions' }, h('button', { class: 'btn small', type: 'button', onclick: cancel }, '取消'), h('button', { class: 'btn small primary', type: 'button', onclick: save }, '保存'), h('small', null, '保存后会标记为“人工”，Agent 不会再改写它')));
  ta.focus();
}

async function toggleHistory(it, box) {
  if (!box.hidden) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.replaceChildren(h('small', null, '加载中…'));
  try {
    const { history } = await get(`/api/memory/items/${it.id}/history`);
    box.replaceChildren(
      ...history.map((v) =>
        h(
          'div',
          { class: 'hv' },
          h('div', { class: 'hv-top' }, h('b', null, 'v' + v.version), h('small', null, fmtFull(v.created_at)), h('small', null, v.actor.startsWith('agent') ? 'Agent' : v.actor.startsWith('user') ? userById(+v.actor.split(':')[1]).display_name : v.actor)),
          h('div', { class: 'hv-body' }, v.content),
          h('small', null, [STATE[v.state] || '', v.adoption && v.adoption !== 'none' ? ADOPT[v.adoption] : '', v.reason].filter(Boolean).join(' · '))
        )
      )
    );
  } catch (e) {
    box.replaceChildren(h('small', null, e.message));
  }
}

async function deleteItem(it) {
  const ok = await ask('删除后不会再显示，并且 Agent 不会再次生成相同的内容。原始聊天不受影响。', { ok: '删除', danger: true, title: '删除这条记忆' });
  if (!ok) return;
  try {
    await del(`/api/memory/items/${it.id}`);
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function textPrompt(title, label, initial, multiline) {
  return new Promise((resolve) => {
    const input = multiline ? h('textarea', { rows: '4', 'aria-label': label }) : h('input', { type: 'text', 'aria-label': label });
    input.value = initial || '';
    let done = false;
    const finish = (v) => {
      if (!done) {
        done = true;
        resolve(v);
      }
    };
    const close = modal({
      title,
      body: h('div', { class: 'form' }, h('label', null, label, input)),
      actions: [h('button', { class: 'btn', type: 'button', onclick: () => (finish(null), close()) }, '取消'), h('button', { class: 'btn primary', type: 'button', onclick: () => (finish(input.value.trim()), close()) }, '确定')],
      onClose: () => finish(null),
    });
  });
}

async function addItem(tp) {
  const v = await textPrompt('添加记忆', '内容', '', true);
  if (!v) return;
  try {
    await post(`/api/rooms/${S.cur}/memory/items`, { topic_id: tp.id, content: v });
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function renameTopic(tp) {
  const v = await textPrompt('重命名主题', '主题名称', tp.title);
  if (!v) return;
  try {
    await patch(`/api/topics/${tp.id}`, { title: v });
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

function mergeTopic(tp, topics) {
  const sel = h('select', { 'aria-label': '合并到' }, topics.filter((t) => t.id !== tp.id).map((t) => h('option', { value: t.id }, t.title)));
  const close = modal({
    title: `把“${tp.title}”合并到…`,
    body: h('div', { class: 'form' }, h('p', null, '条目会一并移动，原主题名会保留为别名。同一会话内才能合并。'), sel),
    actions: [
      h('button', { class: 'btn', type: 'button', onclick: () => close() }, '取消'),
      h(
        'button',
        {
          class: 'btn primary',
          type: 'button',
          onclick: async () => {
            try {
              await post(`/api/topics/${tp.id}/merge`, { into_topic_id: +sel.value });
              close();
              refresh(true);
            } catch (e) {
              toast(e.message, 'err');
            }
          },
        },
        '合并'
      ),
    ],
  });
}

async function archiveTopic(tp) {
  if (!(await ask(`归档“${tp.title}”后它不再出现在列表里，条目仍然保留。`, { ok: '归档' }))) return;
  try {
    await patch(`/api/topics/${tp.id}`, { status: 'archived' });
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ------------------------------------------------------------------ 回顾 / 收藏
async function renderSummaries() {
  const id = S.cur;
  const ok = guard();
  try {
    const { summaries } = await get(`/api/rooms/${id}/summaries`);
    if (!ok()) return;
    els.body.replaceChildren(
      h(
        'div',
        { class: 'sums' },
        ...(summaries.length
          ? summaries.map((s) =>
              h(
                'article',
                { class: 'sum' },
                h('div', { class: 'sum-top' }, h('b', null, s.period_end ? `${fmtDay(s.period_end)} ${fmtTime(s.period_end)}` : fmtDay(s.created_at)), s.stale ? h('span', { class: 'tag warn' }, '待重算') : null),
                h('p', null, s.content),
                h('div', { class: 'srcs' }, s.sources.slice(0, 6).map((x) => h('button', { class: 'src', type: 'button', onclick: () => emit('jump', { id: x.message_id }) }, '原文 #' + x.message_id)))
              )
            )
          : [h('div', { class: 'empty big' }, h('p', null, '还没有回顾'), h('small', null, 'Agent 整理聊天后，会在这里按时间段给出回顾。'))]
        )
      )
    );
  } catch (e) {
    els.body.replaceChildren(h('div', { class: 'empty' }, e.message));
  }
}

async function renderStars() {
  const id = S.cur;
  const ok = guard();
  try {
    const { messages } = await get(`/api/rooms/${id}/stars`);
    if (!ok()) return;
    els.body.replaceChildren(h('div', { class: 'results' }, ...(messages.length ? messages.map(resultItem) : [h('div', { class: 'empty big' }, h('p', null, '还没有收藏'), h('small', null, '在消息右侧的 ⋯ 菜单里可以收藏。'))])));
  } catch (e) {
    els.body.replaceChildren(h('div', { class: 'empty' }, e.message));
  }
}

// ------------------------------------------------------------------ 会话设置
async function setAgent(enabled) {
  try {
    if (!enabled && !(await ask('移出 Agent 后，它将不能再读取这个会话，也不会再整理记忆。已有的主题记忆仍然保留。', { ok: '移出 Agent' }))) return false;
    await post(`/api/rooms/${S.cur}/agent`, { enabled, history: 'from_now' });
    toast(enabled ? 'Agent 已加入（只能看到加入之后的消息）' : 'Agent 已移出');
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function setConsolidation(v) {
  try {
    await patch(`/api/rooms/${S.cur}`, { consolidation_enabled: v });
    refresh(true);
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function renderRoom() {
  const r = room(S.cur);
  const box = h('div', { class: 'rset' });
  box.append(h('h3', null, '成员'));
  box.append(h('div', { class: 'members' }, r.members.map((m) => h('div', { class: 'member' }, avatar(m, 30), h('span', null, m.display_name), m.role === 'agent' ? h('span', { class: 'agent-badge' }, 'Agent') : null, r.type === 'group' && m.role !== 'agent' && m.id !== S.me.id ? iconBtn('close', '移出成员', () => removeMember(m)) : null))));
  if (r.type === 'group') {
    const candidates = S.users.filter((u) => !r.members.some((m) => m.id === u.id));
    if (candidates.length) {
      const sel = h('select', { 'aria-label': '添加成员' }, candidates.map((u) => h('option', { value: u.id }, u.display_name)));
      box.append(
        h(
          'div',
          { class: 'add-member' },
          sel,
          h(
            'button',
            {
              class: 'btn small',
              type: 'button',
              onclick: async () => {
                try {
                  await post(`/api/rooms/${r.id}/members`, { user_id: +sel.value, history: 'from_now' });
                  toast('已添加，新成员只能看到加入之后的消息');
                } catch (e) {
                  toast(e.message, 'err');
                }
              },
            },
            '添加'
          )
        )
      );
    }
  }
  box.append(h('h3', null, 'Agent 与记忆'));
  if (r.type !== 'agent_dm') box.append(toggle('Agent 参与此会话', r.agent_in_room, (v, input) => (setAgent(v).then((ok) => ok === false && (input.checked = !v))), '关闭后 Agent 不能读取这里的任何内容'));
  else box.append(h('p', { class: 'hint' }, '这是你和 Agent 的私聊，Agent 固定参与。'));
  if (r.agent_in_room) box.append(toggle('自动整理主题记忆', r.consolidation_enabled, (v) => setConsolidation(v), '关闭后只保存原始聊天，不再生成主题与回顾'));
  box.append(h('h3', null, '保留与通知'));
  const ret = h('select', { 'aria-label': '消息保留期限' }, [[null, '永久保留'], [30, '30 天'], [90, '90 天'], [365, '1 年']].map(([v, l]) => h('option', { value: v ?? '', selected: (r.retention_days ?? null) === v || null }, l)));
  ret.value = r.retention_days ?? '';
  ret.addEventListener('change', async () => {
    const days = ret.value === '' ? null : +ret.value;
    if (days && !(await ask(`超过 ${days} 天的消息会被永久清除（包括正文、附件和由它们派生的记忆）。这对会话里的所有成员生效。`, { ok: '确认设置', danger: true, title: '设置保留期限' }))) {
      ret.value = r.retention_days ?? '';
      return;
    }
    try {
      await patch(`/api/rooms/${r.id}`, { retention_days: days });
      toast('已更新保留期限');
    } catch (e) {
      toast(e.message, 'err');
    }
  });
  box.append(h('label', { class: 'field' }, h('span', null, '消息保留期限（所有成员）'), ret));
  box.append(toggle('静音（不显示未读提醒）', r.muted, (v) => post(`/api/rooms/${r.id}/mute`, { value: v })));
  box.append(toggle('归档此聊天（仅对我）', r.archived, (v) => post(`/api/rooms/${r.id}/archive`, { value: v }), '归档不删除任何内容，仍可搜索与导出；有新消息时会回到主列表'));
  box.append(h('h3', null, '导出'));
  const fmt = h('select', { 'aria-label': '导出格式' }, h('option', { value: 'md' }, 'Markdown'), h('option', { value: 'json' }, 'JSON'));
  const withMem = h('input', { type: 'checkbox' });
  const withAtt = h('input', { type: 'checkbox' });
  const from = h('input', { type: 'date', 'aria-label': '起始日期' });
  const to = h('input', { type: 'date', 'aria-label': '结束日期' });
  box.append(
    h('div', { class: 'export' }, h('label', { class: 'field' }, h('span', null, '格式'), fmt), h('label', { class: 'check' }, withMem, h('span', null, '包含主题记忆、回顾与变化历史')), h('label', { class: 'check' }, withAtt, h('span', null, '打包附件（zip）')), h('div', { class: 'range' }, from, h('span', null, '至'), to), h('button', { class: 'btn', type: 'button', onclick: () => doExport(fmt.value, withMem.checked, withAtt.checked, from.value, to.value) }, h('span', { class: 'ico' }, icon('download', 16)), '导出'))
  );
  els.body.replaceChildren(box);
}

function doExport(format, mem, att, from, to) {
  const p = { format, memory: mem ? 1 : 0, attachments: att ? 1 : 0 };
  if (from) p.from_time = new Date(from + 'T00:00:00').toISOString();
  if (to) p.to_time = new Date(new Date(to + 'T00:00:00').getTime() + 86400000).toISOString();
  const a = h('a', { href: `/api/rooms/${S.cur}/export${qs(p)}`, download: '' });
  document.body.append(a);
  a.click();
  a.remove();
}

async function removeMember(m) {
  if (!(await ask(`把 ${m.display_name} 移出这个群？`, { ok: '移出', danger: true }))) return;
  try {
    await del(`/api/rooms/${S.cur}/members/${m.id}`);
  } catch (e) {
    toast(e.message, 'err');
  }
}
