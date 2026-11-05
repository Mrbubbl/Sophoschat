# SophosChat

面向双人和小群组的自部署聊天应用。AI 成员在被 @、私聊或收到定向回复时参与讨论，也能在聊天中交付 Markdown 文档和自包含 HTML 网页，保留改稿版本。后台记忆按会话隔离。

当前版本 0.5.0，包含完整 Agent 执行环境、联网工具、外部 MCP、自主注册和服务器本地语音转文字。已实现范围见 [PRD V0.9](docs/PRD-v0.9.md)；最新 [PRD V0.10](docs/PRD-v0.10.md) 规划手动添加联系人与独立联系人列表，保持现有注册方式，功能尚待实现。上游 v9–v20 聊天体验和已实现功能见 [更新日志](docs/RELEASE_NOTES.md)，此前结果见 [V0.8 实施与验收](docs/IMPLEMENTATION-v0.8.md)。

本仓库基于 [Mrbubbl/Sophoschat](https://github.com/Mrbubbl/Sophoschat) 的 `e41aca1` 版本继续开发，保留原始 Git 历史。本轮未新增或更改项目许可证。

## 文档导航

- [部署、升级与备份](docs/DEPLOYMENT.md)
- [账号、Agent、联网与 MCP 使用指南](docs/USAGE.md)
- [服务器本地语音识别：安装模型、配置与使用](docs/ASR.md)
- [手机 PWA、通知与国内推送限制](docs/MOBILE_PUSH.md)
- [测试与验收](docs/TESTING.md)
- [版本说明与已知限制](docs/RELEASE_NOTES.md)

## 启动

需要 Python 3.10+，使用独立虚拟环境。JavaScript 项目执行需要服务器安装 Node.js/npm，浏览器操作需要 Chromium。Claude Agent SDK 包含官方运行时；普通用户只使用网页，无需安装终端 Agent。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[browser,asr]'
python -m playwright install chromium
python -m aichat.asr_setup
# 语音解码还需要 FFmpeg：Linux 使用 sudo apt-get install ffmpeg；macOS 使用 brew install ffmpeg
# Linux 需要时执行：python -m playwright install-deps chromium
python -m aichat.cli init --username alice --display Alice
python -m aichat.cli user add bob --display Bob
python -m aichat.cli serve
```

打开 http://127.0.0.1:8000，Owner 在「设置 → Agent 接入」填写 Claude 模型 ID、API Key、可选 Anthropic 协议网关地址，启用服务端 AI，再运行真实 SDK 与文件自检。密钥仅保存在服务端加密配置中，不回传浏览器。未配置或暂停 AI 时仍可聊天。

AI 工作由服务器托管的 Claude Agent SDK 执行。应用自动启动控制器，为每个任务启动独立执行进程。所有用途都启用完整 Claude Code 工具与 bypassPermissions，不逐次审批。Bash、任意服务账号可访问的文件、Python、npm、依赖安装、测试与构建直接执行，不自动赋予 root/sudo；模型设置与会话仍分别管理。没有其他引擎的自动回退。自定义网关必须支持 SDK 使用的 Anthropic Messages 协议。

默认总并发 2，其中 1 个名额留给互动任务。总并发为 1 时，将互动预留设为 0。Owner 可配置单次费用、轮数、超时及平台/成员/会话月额度。费用未知的执行按预留上限计入预算；SDK 费用是估算，网关账单以服务商记录为准。单次费用限制在一次模型响应结算后生效，并非服务商级的绝对扣费封顶。

## 注册、联网与外部 MCP

登录页点击「没有账号？注册」，填写用户名、昵称和至少 8 位密码；注册后自动登录并建立 Agent 私聊。Owner 可在「设置 → 工具与 MCP」关闭注册。

Agent 内置 internet_search、read_webpage 和联网 browser，默认搜索无需 Key。浏览器支持动态页面、点击、填写、JavaScript 和截图。公共搜索遇到限流/挑战时会明确报错，Owner 可切换 Bing、DuckDuckGo 或配置 Brave/Tavily/SearXNG。

「设置 → 工具与 MCP」支持 HTTP、SSE、stdio 服务，保存后可测试连接和查看工具。日历、云盘等仍需其 MCP 服务和凭证，不自动获得第三方账号。凭证加密保存，所有 Agent 可使用已配置服务，不增加逐工具授权。

任务目录为 data/agent_state/workspaces/room-<id>，跨任务、配置更新和重启保留。终端使用服务账号的实际系统权限；平台 Room API 的检查不构成对完整终端的系统隔离。部署脚本已移除旧只读文件系统限制，服务账号仍受操作系统权限控制。

## 使用

- 私聊、群聊、Agent 私聊；消息引用、编辑、删除、附件、检索、收藏、未读、PWA、实时同步及后台记忆。
- 群聊 @agent 或定向回复 Agent 后参与讨论；引用文字和代码块内的 @ 不触发。
- 请求「制作一份周末计划.md」：先显示任务，文件保存后出现下载卡片。聊天模型建立独立交付任务。
- 点击文件卡「继续修改」：新版本绑定明确旧版本，v1/v2 独立保存、查看和下载。并发改稿遇到旧父版本会报告冲突，防止覆盖新版。
- 请求制作简单网页：交付完整 HTML；预览在无同源权限、无网络的 iframe 中运行。浏览器检查支持 click/fill/expect_text 并记录结果；未验证交互时明确标记。
- 发起人和 Owner 可取消/重试任务；取消使旧租约失效并停止执行进程。已保存的文件保留，重试核查已有交付，避免重复发布。

每个交付任务当前生成一个 `.md` 或 `.html` 文件，默认最大 2 MiB。HTML 支持内联 CSS/JS，不包含远程依赖、嵌套页面或真实后端。下载后在普通浏览器打开，遵循浏览器的常规权限，应用内预览限制不会跟随文件。

## 存储、部署与恢复

FastAPI + SQLite + 原生 JavaScript/PWA。只运行一个应用 worker；公网使用 HTTPS 反向代理，设置 `AICHAT_COOKIE_SECURE=1`。默认数据目录 `./data`，可用 `--data-dir` 或 `AICHAT_DATA_DIR` 更改。自定义端口通过 CLI `serve --port` 启动，以同步 SDK 回连地址；自建 ASGI 启动器需设置 `AICHAT_AGENT_BASE_URL`。

```bash
python -m aichat.cli backup backup.tar.gz
python -m aichat.cli --data-dir ./restored restore backup.tar.gz
python -m aichat.cli --data-dir ./restored doctor
```

完整备份包含数据库、附件、交付文件、解密密钥及私有 SDK 会话，应按私人数据保管。恢复会重新排队中断任务；缺失 SDK 会话文件时控制器重置失效标识，重新读取有权限的上下文。迁移为追加式；升级前备份。

Debian/Ubuntu 安装脚本位于 `deploy/`，只部署应用与随应用启动的 SDK 控制器，升级时停用旧 Bridge。Chromium 和系统依赖仍需按上述命令安装。

## 当前边界

专用 GitHub 连接/分支/PR 产品流程仍属后续阶段；完整终端可使用 Git/CLI，但需要相应远端认证。本地语音转写已接入：长按/右键语音可转文字，无须会话含 Agent；涉及 Agent 的语音自动转写并进入上下文。模型与部署见 [ASR](docs/ASR.md)。已有音视频通话及旧 Notion 接入未纳入本次对齐验收。

按 Room 导出仍使用旧聊天/记忆/附件导出，尚未加入交付物打包；完整备份已包含交付物。专门的交付物删除、ZIP 多文件交付及完整 PRD 验收仍待后续完成。

`aichat_bridge` 保留用于兼容回归和演示，生产默认拒绝其领取任务。显式设置 `AICHAT_ALLOW_LEGACY_AGENT=1` 才能启用旧模式；`scripts/demo.sh` 使用旧模式并关闭 SDK。

## 验证

```bash
pip install -e '.[dev]'
python -m pytest -q
```

自动化测试默认不调用付费模型；接口/进程测试需要本机临时端口和子进程权限。真实网关验证结果记录在实施说明中，不保存 API Key。
