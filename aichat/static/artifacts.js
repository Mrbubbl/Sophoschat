import { get } from './api.js';
import { emit, room, agentUser } from './store.js';
import { h, modal, renderMarkdown, fmtSize, toast, copyText } from './util.js';

function replyArtifact(v) {
  emit('reply-artifact', { id: v.message_id, room_id: v.room_id, body: `${v.filename} · v${v.number}`, sender_id: agentUser(room(v.room_id))?.id });
}

export function artifactCard(v) {
  const browser = v.checks?.browser;
  const check = v.kind === 'html' ? browser?.status === 'passed' ? '浏览器检查通过' : '浏览器交互未验证' : 'UTF-8 与文件校验通过';
  return h('section', { class: 'artifact-card', dataset: { versionId: v.id } },
    h('strong', null, v.filename),
    h('small', null, `v${v.number}${v.parent_version_id ? ' · 基于上一版本' : ''} · ${fmtSize(v.size)}`),
    h('small', { class: 'muted' }, check),
    h('div', { class: 'artifact-actions' },
      h('button', { class: 'btn small', type: 'button', onclick: () => openArtifact(v.id) }, v.kind === 'html' ? '预览网页' : '查看文档'),
      h('a', { class: 'btn small', href: v.download_url, download: v.filename }, '下载'),
      h('button', { class: 'btn small', type: 'button', onclick: () => replyArtifact(v) }, '继续修改')));
}

export async function openArtifact(versionId) {
  try {
    const data = await get(`/api/artifact-versions/${versionId}`);
    let v = data.artifact_version;
    const content = h('div', { class: 'artifact-content' });
    const actions = h('div', { class: 'artifact-toolbar' });
    const versionSelect = h('select', { 'aria-label': '文件版本', onchange: async () => { v = data.versions.find(x => x.id === Number(versionSelect.value)); await render(false); } },
      data.versions.map(x => h('option', { value: x.id, selected: x.id === v.id }, `v${x.number} · ${x.filename}`)));
    const sourceToggle = h('button', { class: 'btn small', type: 'button', onclick: () => render(!showSource) }, '查看源码');
    let showSource = false;
    let generation = 0;
    const render = async (source) => {
      showSource = source;
      const n = ++generation;
      sourceToggle.textContent = source ? '渲染预览' : '查看源码';
      content.replaceChildren(h('p', { class: 'muted' }, '加载中…'));
      if (v.kind === 'html' && !source) {
        content.replaceChildren(h('p', { class: 'note' }, '私有沙箱预览：限制网络、表单外发、弹窗和顶层跳转。下载后打开将使用浏览器的普通权限。'),
          h('iframe', { class: 'artifact-preview', title: v.filename, src: v.preview_url, sandbox: 'allow-scripts', referrerpolicy: 'no-referrer' }));
      } else {
        const text = await get(v.content_url, { text: true });
        if (n !== generation) return;
        content.replaceChildren(source ? h('pre', { class: 'artifact-source' }, h('code', null, text)) : renderMarkdown(text));
      }
      const result = v.checks?.browser;
      if (v.kind === 'html') content.append(h('p', { class: 'note' }, result?.status === 'passed' ? `浏览器检查通过，已验证 ${result.interactions?.length || 0} 项交互。表单默认仅为本地演示。` : '此版本的浏览器交互尚未验证；不能据此认定功能可用。'));
    };
    let close;
    actions.append(versionSelect, sourceToggle,
      h('button', { class: 'btn small', onclick: async () => copyText(await get(v.content_url, { text: true })) }, '复制原文'),
      h('button', { class: 'btn small', onclick: () => { const a = h('a', { href: v.download_url, download: v.filename }); a.click(); } }, '下载此版本'),
      h('button', { class: 'btn primary', onclick: () => { close(); replyArtifact(v); } }, '继续修改'));
    close = modal({ title: data.artifact_version.title, wide: true, body: h('div', { class: 'artifact-viewer' }, actions, content) });
    await render(false);
  } catch (e) { toast(e.message, 'err'); }
}
