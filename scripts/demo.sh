#!/usr/bin/env bash
# 演示：创建 alice/bob 两个账号，启动服务和 mock Agent（规则实现，非真实模型）
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
DATA="${AICHAT_DATA_DIR:-./data-demo}"
PORT="${PORT:-8000}"
CLI="python3 -m aichat.cli --data-dir $DATA"
$CLI init --username alice --display Alice --password 'demo-password-1' 2>/dev/null || true
$CLI user add bob --display Bob --password 'demo-password-2' 2>/dev/null || true
TOKEN=$($CLI agent token --label demo | grep -o 'aca_[^ ]*' | head -1)
echo
echo "打开 http://127.0.0.1:$PORT   账号 alice / demo-password-1  或  bob / demo-password-2"
echo "（同一台电脑上用两个浏览器窗口分别登录，即可体验双人聊天）"
trap 'kill 0' EXIT
AICHAT_CONSOLIDATE_QUIET=10 python3 -m aichat_bridge --url "http://127.0.0.1:$PORT" --token "$TOKEN" --adapter mock --guide-path "$DATA/AGENT_GUIDE.md" &
AICHAT_CONSOLIDATE_QUIET=10 $CLI serve --port "$PORT"
