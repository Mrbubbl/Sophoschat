"""浏览器端到端验证：真实服务 + 真实 Bridge（mock 适配器）+ 两个浏览器会话。
用法：python3 scripts/e2e.py [输出截图目录]
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/e2e-shots"
PORT = int(os.environ.get("E2E_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"
os.makedirs(OUT, exist_ok=True)
DATA = tempfile.mkdtemp(prefix="aichat-e2e-")
env = {**os.environ, "PYTHONPATH": ROOT, "AICHAT_CONSOLIDATE_QUIET": "2", "AICHAT_SCHEDULER_TICK": "1", "AICHAT_CHAT_TASK_TTL": "120", "AICHAT_AGENT_OFFLINE_AFTER": "6", "AICHAT_HEARTBEAT_SECONDS": "2"}
procs = []
errors = []


def run(*args):
    return subprocess.run([sys.executable, "-m", "aichat.cli", "--data-dir", DATA, *args], env=env, capture_output=True, text=True, check=True).stdout


def start(cmd, name):
    p = subprocess.Popen(cmd, env=env, stdout=open(f"{DATA}/{name}.log", "a"), stderr=subprocess.STDOUT, cwd=ROOT)
    procs.append(p)
    return p


PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082")


def wait_http():
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.25)
    raise SystemExit("服务没有启动：\n" + open(f"{DATA}/server.log").read())


def shot(page, name):
    page.screenshot(path=f"{OUT}/{name}.png")
    print("  截图", name)


def login(page, user, pw):
    page.goto(BASE)
    page.wait_for_selector("input[name=username]")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", pw)
    page.click("button[type=submit]")
    page.wait_for_selector(".room-item", timeout=10000)


def watch(page, tag):
    page.on("pageerror", lambda e: errors.append(f"[{tag}] pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"[{tag}] console: {m.text}"))


def send(page, text):
    page.fill("#input", text)
    page.press("#input", "Enter")


def open_room(page, title):
    page.click(f".room-item:has(.ri-title:text-is('{title}'))")
    page.wait_for_selector(".chat-head h1:text-is('" + title + "')")


try:
    print("初始化数据与账号")
    run("init", "--username", "alice", "--display", "Alice", "--password", "password-alice")
    run("user", "add", "bob", "--display", "Bob", "--password", "password-bob")
    token = re.search(r"aca_\S+", run("agent", "token", "--label", "e2e")).group(0)
    start([sys.executable, "-m", "aichat.cli", "--data-dir", DATA, "serve", "--port", str(PORT)], "server")
    wait_http()
    bridge_cmd = [sys.executable, "-m", "aichat_bridge", "--url", BASE, "--token", token, "--adapter", "mock", "--guide-path", f"{DATA}/AGENT_GUIDE.md"]
    bridge = start(bridge_cmd, "bridge")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx_a = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        ctx_b = browser.new_context(viewport={"width": 1200, "height": 800}, locale="zh-CN")
        pa, pb = ctx_a.new_page(), ctx_b.new_page()
        watch(pa, "alice")
        watch(pb, "bob")

        print("1. 登录页与登录")
        pa.goto(BASE)
        pa.wait_for_selector("input[name=username]")
        shot(pa, "01-login")
        pa.fill("input[name=username]", "alice")
        pa.fill("input[name=password]", "wrong-password")
        pa.click("button[type=submit]")
        pa.wait_for_selector(".login-err:not(:empty)")
        assert "错误" in pa.inner_text(".login-err")
        login(pa, "alice", "password-alice")
        login(pb, "bob", "password-bob")
        titles = pa.locator(".ri-title").all_inner_texts()
        print("  Alice 的会话:", titles)
        assert "Bob" in titles and "Agent" in titles

        print("2. 双人实时聊天")
        open_room(pa, "Bob")
        open_room(pb, "Alice")
        send(pa, "你好 Bob，我想夏天去海边旅行")
        pb.wait_for_selector(".row.other .body:has-text('海边旅行')", timeout=8000)
        send(pb, "好呀，预算 6000 以内")
        pa.wait_for_selector(".row.other .body:has-text('预算 6000')", timeout=8000)
        shot(pa, "02-chat")

        print("3. @Agent 在双人会话里回复；默认不 @ 不打扰")
        send(pa, "没有 @ 的普通消息，Agent 不应该回复")
        time.sleep(2.5)
        assert pa.locator(".row.agent").count() == 0, "未 @ 时 Agent 不应发言"
        send(pa, "@agent 你有什么建议？")
        pa.wait_for_selector(".row.agent .body:has-text('演示 Agent')", timeout=15000)
        shot(pa, "03-agent-reply")

        print("4. Agent 私聊")
        open_room(pa, "Agent")
        send(pa, "你好，Agent")
        pa.wait_for_selector(".row.agent .body:has-text('收到')", timeout=15000)

        print("5. 编辑 / 回复 / 收藏 / 删除")
        open_room(pa, "Bob")
        pa.hover(".row.own >> nth=0")
        pa.click(".row.own >> nth=0 >> .tool >> nth=1")
        pa.click(".popbar button:has-text('编辑')")
        pa.fill(".edit-box", "你好 Bob，我想夏天去海边旅行（改）")
        pa.press(".edit-box", "Enter")
        pa.wait_for_selector(".row.own .edited")
        pa.hover(".row.other >> nth=0")
        pa.click(".row.other >> nth=0 >> .tool >> nth=1")
        pa.click(".popbar button:has-text('收藏')")
        pa.wait_for_selector(".row.other .starred")
        pa.hover(".row.other >> nth=0")
        pa.click(".row.other >> nth=0 >> .tool >> nth=1")
        pa.click(".popbar button:has-text('引用')")
        send(pa, "收到你的预算啦")
        pa.wait_for_selector(".reply:has-text('预算')")
        shot(pa, "04-reply-edit")

        print("6. 附件")
        pa.set_input_files("input[type=file]", {"name": "报价单.txt", "mimeType": "text/plain", "buffer": b"hello"})
        pa.wait_for_selector(".attbar .fname:has-text('报价单.txt')")
        send(pa, "附件在这")
        pb.wait_for_selector(".att-file:has-text('报价单.txt')", timeout=8000)

        print("7. 后台整理 → 主题记忆")
        pa.click("button[aria-label='回顾与主题记忆']") if pa.locator(".panel").is_hidden() else None
        pa.wait_for_selector(".panel")
        pa.click(".ptab:has-text('主题记忆')")
        pa.wait_for_selector(".topic-head:has-text('旅行相关')", timeout=40000)
        shot(pa, "05-memory")
        pa.click(".topic-head:has-text('旅行相关')")
        pa.wait_for_selector(".item .content")
        shot(pa, "06-memory-open")
        # 用户手工纠正后标记为“人工”
        pa.click(".item >> nth=0 >> button:has-text('编辑')")
        pa.fill(".item .edit-box", "两个人都想夏天去海边（人工确认）")
        pa.click(".item button:has-text('保存')")
        pa.wait_for_selector(".tag.manual")
        # 点击来源跳转回原文
        pa.click(".item >> nth=0 >> .src")
        pa.wait_for_selector(".row.hl", timeout=8000)

        print("8. 设置与 Agent 接入")
        pa.click(".pill")
        pa.wait_for_selector(".agent-card")
        assert "在线" in pa.inner_text(".agent-card .ac-top")
        shot(pa, "07-settings-agent")
        pa.click(".steps button:has-text('运行接入自检')")
        pa.wait_for_selector(".check-row.ok")
        pa.keyboard.press("Escape")

        print("9. 搜索 / 导出")
        pa.fill("#gsearch", "海边")
        pa.wait_for_selector(".result:has-text('海边')")
        shot(pa, "08-search")
        pa.fill("#gsearch", "")
        pa.click(".ptab:has-text('会话设置')")
        pa.wait_for_selector(".rset .export")
        with pa.expect_download() as dl:
            pa.click(".export button:has-text('导出')")
        path = dl.value.path()
        assert os.path.getsize(path) > 100
        shot(pa, "09-room-settings")

        print("10. 手机视图")
        ctx_m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, locale="zh-CN")
        pm = ctx_m.new_page()
        watch(pm, "mobile")
        login(pm, "bob", "password-bob")
        shot(pm, "10-mobile-list")
        pm.click(".room-item >> nth=0")
        pm.wait_for_selector(".chat-head")
        shot(pm, "11-mobile-chat")
        pm.click('.act-toggle'); pm.click("button[aria-label='回顾与主题记忆']")
        pm.wait_for_selector(".ptab")
        shot(pm, "12-mobile-panel")

        print("11x. 恶意内容不会被执行（XSS）")
        open_room(pa, "Bob")
        send(pa, '<img src=x onerror="window.__xss=1"> [点我](javascript:window.__xss=1) **粗体** `code` https://example.com/a')
        pa.wait_for_selector(".row.own .body strong:text-is('粗体')")
        open_room(pb, "Alice")
        pb.wait_for_selector(".row.other .body strong:text-is('粗体')", timeout=8000)
        for pg in (pa, pb):
            assert pg.evaluate("window.__xss") is None, "脚本被执行了"
            assert pg.locator("a[href^='javascript']").count() == 0
            assert pg.locator(".row .body img").count() == 0
            assert "<img src=x" in pg.locator(".row .body").last.inner_text()
            assert pg.locator(".row .body a[href='https://example.com/a']").count() >= 1

        print("11a. 未读角标、未读分隔线")
        open_room(pb, "Agent")
        send(pa, "未读测试：Bob 不在这个会话")
        pb.wait_for_selector(".room-item:has(.ri-title:text-is('Alice')) .badge:text-is('1')", timeout=8000)
        open_room(pb, "Alice")
        pb.wait_for_selector(".unread-divider")
        pb.wait_for_function("() => !document.querySelector('.room-item .badge')", timeout=8000)

        print("11b. 图片、灯箱、@ 提及选择器")
        open_room(pa, "Bob")
        pa.set_input_files("input[type=file]", {"name": "pic.png", "mimeType": "image/png", "buffer": PNG})
        pa.wait_for_selector(".attbar .fname:has-text('pic.png')")
        send(pa, "")
        pb.wait_for_selector(".att-img", timeout=8000)
        pb.locator(".att-img").last.dispatch_event("click")  # 测试图只有 1 像素，真实点击会落到相邻元素上
        pb.wait_for_selector(".lightbox")
        pb.click(".lightbox")
        pa.fill("#input", "@ag")
        pa.wait_for_selector(".mention-pop button")
        pa.press("#input", "Enter")
        assert pa.input_value("#input").startswith("@agent "), pa.input_value("#input")
        pa.fill("#input", "")

        print("11c. 离线发送失败后自动重试")
        ctx_a.set_offline(True)
        send(pa, "Alice 离线时发出的消息")
        pa.wait_for_selector(".row.pending .fail", timeout=8000)
        shot(pa, "13-send-failed")
        ctx_a.set_offline(False)
        pb.wait_for_selector(".row.other .body:has-text('Alice 离线时发出的消息')", timeout=40000)

        print("11d. Agent 离线 → 消息保留并提示 → 恢复后处理")
        bridge.send_signal(signal.SIGTERM)
        bridge.wait(timeout=10)
        pa.wait_for_selector(".pill:has-text('离线')", timeout=20000)
        open_room(pa, "Agent")
        pa.wait_for_selector(".agentbar:has-text('离线')")
        send(pa, "Agent 离线时发的问题")
        pa.wait_for_selector(".chip.warn:has-text('Agent 离线')", timeout=8000)
        shot(pa, "14-agent-offline")
        start(bridge_cmd, "bridge")
        pa.wait_for_selector(".row.agent .body:has-text('Agent 离线时发的问题')", timeout=40000)
        pa.wait_for_selector(".pill:has-text('在线')", timeout=20000)

        print("11. 断线重连后补齐消息")
        open_room(pa, "Bob")
        ctx_b.set_offline(True)
        time.sleep(1)
        send(pa, "Bob 离线时发的消息")
        time.sleep(1.5)
        ctx_b.set_offline(False)
        pb.wait_for_selector(".row.other .body:has-text('Bob 离线时发的消息')", timeout=30000)
        ctx_d = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark", locale="zh-CN")
        pd = ctx_d.new_page()
        watch(pd, "dark")
        login(pd, "alice", "password-alice")
        open_room(pd, "Bob")
        pd.wait_for_selector(".row .body")
        shot(pd, "15-dark")
        browser.close()

    print("\n控制台/页面错误：", errors or "无")
    bad = [e for e in errors if "ERR_INTERNET_DISCONNECTED" not in e and "WebSocket" not in e and "Failed to load resource" not in e]
    if bad:
        raise SystemExit("发现前端错误:\n" + "\n".join(bad))
    print("端到端验证通过")
finally:
    for p in procs:
        p.send_signal(signal.SIGTERM)
    for p in procs:
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
    for name in ("server", "bridge"):
        try:
            tail = open(f"{DATA}/{name}.log").read()[-1500:]
            if "Traceback" in tail or os.environ.get("E2E_LOGS"):
                print(f"--- {name}.log ---\n{tail}")
        except Exception:
            pass
    shutil.rmtree(DATA, ignore_errors=True)
