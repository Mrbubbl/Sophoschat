# 测试与验收

## 自动化回归

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q
```

自动化测试不调用付费模型。接口测试需要本机临时端口，MCP/取消测试会启动临时子进程，故受限环境需要允许这些操作。

2026-11-02 的完整回归结果为 **138 项通过**；上传前再次运行并核对结果。主要覆盖：

- 原有聊天、记忆、导出、附件、会话和推送逻辑。
- 租约代次、旧令牌失效、权限过滤、取消及实际进程清理。
- SDK 配置加密、预算预留、用量汇总、会话恢复、自检真实回执。
- Markdown/HTML 文件、不可变改稿版本、父版本冲突、幂等发布、备份恢复与访问检查。
- 自主注册、角色注入拒绝、密码/重名/来源/频率检查及 Owner 关闭注册。
- 真实 stdio MCP 数据写入读回、HTTP MCP 认证握手、配置加密与密钥保留。

测试通过不代表所有聊天语义、厂商网关、操作系统和移动推送场景都已验证。

## 真实 SDK 与网关验证

使用配置的 Anthropic 兼容网关，对以下操作保留过真实回执：

1. 对话委派任务、Markdown 初稿与 v2 改稿、v1 内容保持。
2. HTML 保存，以及 Chromium 实际点击后数字变化的断言。
3. Python 测试、npm 安装依赖、npm test、npm run build 及实际构建文件。
4. 互联网搜索、浏览器打开真实网页、JavaScript 与截图。
5. 外部 MCP append_note / list_notes 实际写入读回。
6. 最终服务端 SDK 文件自检。

网关曾返回 502；失败尝试没有当作成功。SDK 费用是估算，中断后缺失回执的费用按未知处理。本仓库不包含真实 Key、浏览器会话、原始 SDK transcript 或本地测试数据。

## 浏览器验收脚本

`scripts/e2e_v7.py` 验证文件卡、版本、下载、继续修改、HTML 沙箱与 Owner 设置；需要预先准备含 `test-plan.md` v1/v2 及 `counter.html` 的独立测试实例。

`scripts/e2e_v8.py` 验证注册、自动登录、MCP 编辑器、连接测试和注册开关。它会创建测试账号，并修改注册/连接配置，只能对独立测试实例执行。需预先配置名为 `testnotes` 的真实测试 MCP，例如使用 `tests/fixtures/notes_mcp.py`，设置服务端 `NOTES_PATH` 为临时文件。

```bash
python scripts/e2e_v8.py \
  --url http://127.0.0.1:18763 \
  --login-file /safe/private/test-owner-login.json \
  --browser /actual/path/to/chromium \
  --out /tmp/sophoschat-e2e
```

login-file 格式为 `{"username":"你的测试账号","password":"测试密码"}`。文件只放在私人目录，不提交 Git。截图和原始日志留在本地 `output/` 或指定输出目录，已从 Git 忽略。

## 人工验收

参照 [使用指南](USAGE.md) 测试注册、私聊、群聊、文件与改稿；参照 [手机推送说明](MOBILE_PUSH.md) 做真机锁屏和后台测试。没有 MCP 凭证时应显示缺少连接，不能虚构日历或云盘操作成功。
