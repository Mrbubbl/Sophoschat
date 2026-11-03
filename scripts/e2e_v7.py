"""Read-only browser acceptance against a prepared local SDK test instance.

Requires a Room containing test-plan.md v1/v2 and counter.html from real SDK
delivery. Credentials come from --login-file, never a source-code constant.
Does not call the model or save credentials in screenshots.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

p = argparse.ArgumentParser()
p.add_argument('--url', default='http://127.0.0.1:18763')
p.add_argument('--login-file', required=True)
p.add_argument('--browser', required=True)
p.add_argument('--out', required=True)
args = p.parse_args()
login = json.loads(Path(args.login_file).read_text())
out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
errors = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=args.browser, headless=True, chromium_sandbox=True)
    context = browser.new_context(viewport={'width':1440,'height':1000}, accept_downloads=True)
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(args.url)
    page.get_by_label('用户名').fill(login['username'])
    page.get_by_label('密码',exact=True).fill(login['password'])
    page.locator('button[type=submit]').click()
    page.locator('.room-item').first.click()
    md = page.locator('.artifact-card').filter(has_text='test-plan.md').last
    md.get_by_role('button',name='查看文档',exact=True).click()
    viewer = page.locator('.artifact-viewer')
    expect(viewer.get_by_label('文件版本').locator('option')).to_have_count(2)
    expect(viewer.locator('.artifact-content')).to_contain_text('周末计划')
    page.screenshot(path=str(out/'markdown-viewer.png'))
    viewer.get_by_role('button',name='查看源码',exact=True).click()
    expect(viewer.locator('pre')).to_contain_text('#')
    with page.expect_download() as d:
        viewer.get_by_role('button',name='下载此版本',exact=True).click()
    assert d.value.suggested_filename=='test-plan.md'
    viewer.get_by_role('button',name='继续修改',exact=True).click()
    expect(page.get_by_role('button',name='取消回复',exact=True)).to_be_visible()
    html = page.locator('.artifact-card').filter(has_text='counter.html').last
    html.get_by_role('button',name='预览网页',exact=True).click()
    iframe = page.locator('iframe.artifact-preview')
    expect(iframe).to_have_attribute('sandbox','allow-scripts')
    frame = page.frame_locator('iframe.artifact-preview')
    frame.locator('button').first.click()
    assert frame.locator('body').inner_text()
    # A sandboxed artifact must not read the parent application's DOM/cookies.
    assert iframe.element_handle().content_frame().evaluate("() => {try {void parent.document.body; return false} catch {return true}}")
    page.screenshot(path=str(out/'html-preview.png'))
    page.locator('.modal-head').get_by_role('button',name='关闭',exact=True).click()
    page.get_by_title('设置',exact=True).click()
    page.get_by_role('tab',name='Agent 接入',exact=True).click()
    expect(page.get_by_label('默认 Claude 模型 ID')).to_be_visible()
    assert page.get_by_label('服务端 API Key',exact=True).input_value()==''
    page.screenshot(path=str(out/'owner-agent-settings.png'))
    assert not errors,errors
    context.close();browser.close()
print('Browser acceptance passed: card, Markdown, versions, download, reply, HTML sandbox, Owner settings')
