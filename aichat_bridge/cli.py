"""aichat-bridge：在你控制的设备上常驻运行外部 Agent。"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import time

from .adapters import make_adapter
from .client import BridgeApiError
from .runner import Bridge


def main(argv=None):
    p = argparse.ArgumentParser(prog="aichat-bridge", description="SophosChat 的 Agent Bridge")
    p.add_argument("--url", default=os.environ.get("AICHAT_URL", "http://127.0.0.1:8000"))
    p.add_argument("--token", default=os.environ.get("AICHAT_AGENT_TOKEN"), help="应用凭证 aca_…（也可用环境变量 AICHAT_AGENT_TOKEN）")
    p.add_argument("--adapter", default=os.environ.get("AICHAT_ADAPTER", "mock"), choices=["mock", "openai", "anthropic", "command"])
    p.add_argument("--model", default=os.environ.get("AICHAT_LLM_MODEL"))
    p.add_argument("--command", default=os.environ.get("AICHAT_LLM_COMMAND"), help='command 适配器的命令，提示词走标准输入，例如 "codex exec -"')
    p.add_argument("--guide-path", default="AGENT_GUIDE.md", help="本地 AGENT_GUIDE 副本的保存位置")
    p.add_argument("--once", action="store_true", help="只处理一个任务后退出（调试用）")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not a.token:
        raise SystemExit("缺少凭证：--token 或环境变量 AICHAT_AGENT_TOKEN（在网页「设置 → Agent 接入」或 `aichat agent token` 获取）")
    adapter = make_adapter(a.adapter, model=a.model, command=a.command)
    b = Bridge(a.url, a.token, adapter, owner=socket.gethostname(), guide_path=a.guide_path)
    for attempt in range(60):  # 服务可能还没启动完：等待而不是直接退出
        try:
            b.heartbeat()
            break
        except BridgeApiError as e:
            if e.status in (401, 403):
                raise SystemExit(f"凭证无效或已撤销（{e.code}）：请在 设置 → Agent 接入 重新创建凭证")
            if attempt == 0:
                logging.warning("暂时连不上 %s（%s），重试中…", a.url, e.message)
            time.sleep(2)
    else:
        raise SystemExit(f"一直连不上 {a.url}，请确认服务已启动且地址正确")
    if a.once:
        b.run_once(wait=5)
        return
    logging.info("Bridge 已启动：适配器=%s，服务=%s", adapter.name, a.url)
    try:
        b.run_forever()
    except KeyboardInterrupt:
        b.stop()


if __name__ == "__main__":
    main()
