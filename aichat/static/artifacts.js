import { get } from './api.js';
import { emit, room, agentUser } from './store.js';
import { h, icon, iconBtn, modal, renderMarkdown, fmtSize, fmtRel, toast, copyText, popMenu } from './util.js';

function replyArtifact(v) {
  emit('reply-artifact', { id: v.message_id, room_id: v.room_id, body: `${v.filename} · v${v.number}`, sender_id: agentUser(room(v.room_id))?.id });
}

const isVerified = (v) => v.checks?.browser?.status === 'passed';

// 聊天里的交付文件卡片：文件名和版本在上，一个主操作 + 两个轻量操作
export function artifactCard(v) {
  const meta = [`v${v.number}`, fmtSize(v.size)];
  if (v.kind === 'html' && !isVerified(v)) meta.push('交互未验证');
  return h(
    'section',
    { class: 'artifact-card', dataset: { versionId: v.id } },
    h('div', { class: 'ac-head' }, h('span', { class: 'ac-ico' }, icon(v.kind === 'html' ? 'panel' : 'edit', 18)), h('div', { class: 'ac-text' }, h('strong', null, v.filename), h('small', { class: 'muted' }, meta.join(' · ')))),
    h(
      'div',
      { class: 'ac-actions' },
      h('button', { class: 'btn small', type: 'button', onclick: () => openArtifact(v.id) }, v.kind === 'html' ? '预览' : '查看'),
      h('a', { class: 'ac-link', href: v.download_url, download: v.filename }, '下载'),
      h('button', { class: 'ac-link', type: 'button', onclick: () => replyArtifact(v) }, '继续修改')
    )
  );
}

export async function openArtifact(versionId) {
  try {
    const data = await get(`/api/artifact-versions/${versionId}`);
    let v = data.artifact_version;
    const versions = data.versions || [v];
    let showSource = false;
    let generation = 0;
    let close = () => {};

    const stage = h('div', { class: 'av-stage' });
    const titleEl = h('div', { class: 'av-title' });
    const subEl = h('div', { class: 'av-sub' });
    const fullSlot = h('span', { class: 'av-slot' });
    const note = h('div', { class: 'av-note', hidden: true });
    const chipSlot = h('span', { class: 'av-slot' });
    const seg = h('div', { class: 'av-seg', role: 'tablist' });

    const verBtn = () => {
      const label = [`v${v.number}`, fmtSize(v.size), fmtRel(v.created_at)].join(' · ');
      if (versions.length < 2) return h('span', null, label);
      const b = h('button', { class: 'av-ver', type: 'button', 'aria-haspopup': 'menu', 'aria-label': '切换版本' }, label, icon('down', 12));
      b.addEventListener('click', () =>
        popMenu(
          b,
          versions.map((x) => ({
            label: `v${x.number} · ${x.filename}`,
            on: x.id === v.id,
            fn: () => {
              v = x;
              showSource = false;
              render();
            },
          }))
        )
      );
      return b;
    };

    const header = () => {
      titleEl.textContent = data.artifact_version.title || v.filename;
      subEl.replaceChildren(verBtn());
      fullSlot.replaceChildren(...(v.kind === 'html' ? [h('a', { class: 'icon-btn', href: v.preview_url, target: '_blank', rel: 'noopener noreferrer', title: '全屏打开（仍在隔离沙箱中运行）', 'aria-label': '全屏打开' }, icon('expand', 19))] : []));
      seg.replaceChildren(
        h('button', { type: 'button', role: 'tab', class: showSource ? '' : 'on', 'aria-selected': String(!showSource), onclick: () => ((showSource = false), render()) }, v.kind === 'html' ? '预览' : '阅读'),
        h('button', { type: 'button', role: 'tab', class: showSource ? 'on' : '', 'aria-selected': String(showSource), onclick: () => ((showSource = true), render()) }, '源码')
      );
      note.hidden = true;
      if (v.kind === 'html') {
        const ok = isVerified(v);
        const chip = h('button', { class: 'av-chip' + (ok ? ' ok' : ' warn'), type: 'button', 'aria-expanded': 'false' }, icon('shield', 14), ok ? '沙箱运行 · 已验证' : '沙箱运行 · 交互未验证');
        note.textContent = ok
          ? `在隔离沙箱中运行，限制联网、表单外发和弹窗。浏览器检查已通过，验证了 ${v.checks.browser.interactions?.length || 0} 项交互。`
          : '在隔离沙箱中运行，限制联网、表单外发和弹窗。这个版本的交互还没有经过浏览器验证，功能是否可用请自己试一下。下载后用浏览器打开则是普通权限。';
        chip.addEventListener('click', () => {
          note.hidden = !note.hidden;
          chip.setAttribute('aria-expanded', String(!note.hidden));
        });
        chipSlot.replaceChildren(chip);
      } else chipSlot.replaceChildren(h('span', { class: 'av-chip ok static' }, icon('shield', 14), '文件校验通过'));
    };

    const render = async () => {
      header();
      const n = ++generation;
      if (v.kind === 'html' && !showSource) {
        stage.replaceChildren(h('iframe', { class: 'av-frame', title: v.filename, src: v.preview_url, sandbox: 'allow-scripts', referrerpolicy: 'no-referrer' }));
        return;
      }
      stage.replaceChildren(h('p', { class: 'muted av-loading' }, '加载中…'));
      try {
        const text = await get(v.content_url, { text: true });
        if (n !== generation) return;
        stage.replaceChildren(showSource ? h('pre', { class: 'av-source' }, h('code', null, text)) : h('div', { class: 'av-doc' }, renderMarkdown(text)));
      } catch (e) {
        if (n === generation) stage.replaceChildren(h('p', { class: 'muted av-loading' }, e.message));
      }
    };

    const moreBtn = iconBtn('more', '更多', () =>
      popMenu(moreBtn, [
        { label: '复制原文', icon: 'copy', fn: async () => copyText(await get(v.content_url, { text: true })) },
        { label: '下载此版本', icon: 'download', fn: () => h('a', { href: v.download_url, download: v.filename }).click() },
        ...(v.kind === 'html' ? [{ label: '全屏打开', icon: 'expand', fn: () => window.open(v.preview_url, '_blank', 'noopener,noreferrer') }] : []),
      ])
    );
    const top = h('div', { class: 'av-top' }, iconBtn('close', '关闭', () => close()), h('div', { class: 'av-heading' }, titleEl, subEl), fullSlot, moreBtn);
    const bar = h(
      'div',
      { class: 'av-bar' },
      h('button', { class: 'av-ghost', type: 'button', 'aria-label': '下载此版本', title: '下载此版本', onclick: () => h('a', { href: v.download_url, download: v.filename }).click() }, icon('download', 20)),
      h('button', { class: 'av-primary', type: 'button', onclick: () => (close(), replyArtifact(v)) }, icon('edit', 18), '继续修改')
    );

    close = modal({ title: data.artifact_version.title, wide: true, sheet: true, body: h('div', { class: 'av' }, top, h('div', { class: 'av-tools' }, seg, h('span', { class: 'grow' }), chipSlot), note, stage, bar) });
    await render();
  } catch (e) {
    toast(e.message, 'err');
  }
}
