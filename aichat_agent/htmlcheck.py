"""Bounded browser smoke test; no login state, network, or arbitrary eval tool."""
from __future__ import annotations

import asyncio
import json
import os
import sys


async def check_html(content: str, interactions: list) -> dict:
    from .service import terminate_tree
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "PYTHONPATH", "AICHAT_CHROMIUM_PATH")}
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "aichat_agent.htmlcheck", "--worker", env=env,
                 start_new_session=True, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        payload = json.dumps({"content": content, "interactions": interactions[:10], "controller_pid": os.getpid()}).encode()
        out, _ = await asyncio.wait_for(proc.communicate(payload), 15)
        return json.loads(out) if proc.returncode == 0 else {"status": "not_verified", "reason": "browser_unavailable"}
    except asyncio.TimeoutError:
        return {"status": "failed", "reason": "browser_timeout"}
    except (ValueError, OSError):
        return {"status": "not_verified", "reason": "browser_unavailable"}
    finally:
        await terminate_tree(proc)


def verify(data: dict) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            kwargs = {"headless": True, "chromium_sandbox": True}
            if os.environ.get("AICHAT_CHROMIUM_PATH"):
                kwargs["executable_path"] = os.environ["AICHAT_CHROMIUM_PATH"]
            try:
                browser = pw.chromium.launch(**kwargs)
            except Exception:
                return {"status": "not_verified", "reason": "browser_unavailable"}
            try:
                context = browser.new_context(accept_downloads=False, service_workers="block", permissions=[])
                page = context.new_page()
                page.set_default_timeout(2000)
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)[:200]))
                csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'; sandbox allow-scripts"
                def route(r):
                    if r.request.url == "https://artifact.invalid/":
                        r.fulfill(status=200, content_type="text/html", body=data["content"], headers={"Content-Security-Policy": csp})
                    else:
                        r.abort()
                page.route("**/*", route)
                page.goto("https://artifact.invalid/", wait_until="load", timeout=3000)
                checks = []
                for step in data["interactions"]:
                    locator = page.locator(step["selector"])
                    if step["action"] == "click":
                        locator.click()
                    elif step["action"] == "fill":
                        locator.fill(step.get("value", ""))
                    elif step["action"] == "expect_text":
                        if step.get("text", "") not in locator.inner_text():
                            raise ValueError("interaction_assertion_failed")
                    else:
                        raise ValueError("invalid_interaction")
                    checks.append({"action": step["action"], "selector": step["selector"], "passed": True})
                if errors:
                    return {"status": "failed", "reason": "javascript_error", "errors": errors, "interactions": checks}
                return {"status": "passed", "title": page.title()[:120], "interactions": checks, "network": "blocked"}
            finally:
                browser.close()
    except ImportError:
        return {"status": "not_verified", "reason": "playwright_not_installed"}
    except Exception:
        return {"status": "failed", "reason": "browser_or_interaction_failed"}


if __name__ == "__main__":
    data = json.loads(sys.stdin.read())
    from .watchdog import watch_parent
    watch_parent(data["controller_pid"])
    print(json.dumps(verify(data), ensure_ascii=False))
