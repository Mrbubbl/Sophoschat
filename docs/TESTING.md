# 测试与验收

## 自动化回归

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q
```

自动化测试不调用付费模型。接口测试需要本机临时端口，MCP/取消测试会启动临时子进程，故受限环境需要允许这些操作。

2026-11-05，应用 0.6.0 的完整回归结果为 **178 项通过**（0.5.0 为 165 项，0.4.0 为 138 项）。主要覆盖：

- 原有聊天、记忆、导出、附件、会话和推送逻辑。
- 租约代次、旧令牌失效、权限过滤、取消及实际进程清理。
- SDK 配置加密、预算预留、用量汇总、会话恢复、自检真实回执。
- Markdown/HTML 文件、不可变改稿版本、父版本冲突、幂等发布、备份恢复与访问检查。
- 自主注册、角色注入拒绝、密码/重名/来源/频率检查及 Owner 关闭注册。
- 真实 stdio MCP 数据写入读回、HTTP MCP 认证握手、配置加密与密钥保留。
- 本地转写权限与历史边界、缓存和队列限制、删除中任务、失败重试、旧代次结果丢弃、超时进程回收、重启恢复及 Agent 等待/读取文字。

联系人新增覆盖手动添加、精确查找/限流、个人目录权限、初始化修正、空私聊可见性、单向回复、多设备同步和旧库迁移保护。

测试通过不代表所有聊天语义、厂商网关、操作系统和移动推送场景都已验证。

## 联系人验收

新增 `tests/test_contacts.py` 13 项，覆盖 PRD V0.10 的初始化、单向添加/移除、越权、停用账号、查找限流、群成员、双设备 WebSocket、CLI、迁移预览/执行及备份恢复。旧测试显式建立测试联系人，避免依赖已取消的全员自动私聊。

```bash
python -m pytest -q tests/test_contacts.py
python scripts/verify_contacts.py
# 需要浏览器检查时加 --keep-running；随机测试登录信息仅保存在忽略的 output/ 中
```

验收脚本使用独立数据库和真实 HTTP 验证注册、添加、发送、回复、移除、历史及目录访问。桌面与 390×844 手机视口已检查空列表、查找/添加、联系人搜索、资料卡、消息、移除保留历史、重新添加、联系人建群和返回导航，控制台无错误。未做实体手机系统手势、弱网或通知送达率测试。详细证据见 [V0.10 实施记录](IMPLEMENTATION-v0.10.md)。

## 本地语音识别验收

先按 [ASR 安装指南](ASR.md) 安装可选运行时和模型，再运行独立验收。它启动临时服务器和数据库，生成随机测试账号，不调用付费语言模型，也不修改正式数据：

```bash
python scripts/verify_local_asr.py \
  --model-dir data/models/sensevoice-small-int8 \
  --out output/asr-acceptance
```

2026-11-04，在 macOS arm64、Python 3.12、sherpa-onnx 2.0.1、CPU 两线程上，用官方公开样例重新编码后通过实际上传/转写接口验证：

| 音频 | 格式 | 请求到完成 |
| --- | --- | --- |
| 普通话 5.59 秒 | WebM / Opus | 1.28 秒（含首次加载） |
| 英文 7.15 秒 | Ogg / Opus | 0.75 秒 |
| 粤语 5.15 秒 | M4A / AAC | 0.53 秒 |

同时验证重复请求复用、无额外聊天消息、损坏音频/静音明确失败，以及无手动请求的 Agent 语音转写实际进入任务上下文。原始结果写入本地 `report.json`。这些短样例不能代表生产吞吐和识别准确率；普通话将“开放”识别成“开饭”，英文将“gold”识别成“code”，保留原始结果，不作为精确识别通过。

浏览器实测无 Agent 双人聊天：右键“转文字”→处理中→文字显示；隐藏/恢复文字正常，设置页显示本地服务已就绪。未做实体手机长按、弱网和长录音质量评测；移动端复用现有长按菜单入口，仍需真机补验。

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
