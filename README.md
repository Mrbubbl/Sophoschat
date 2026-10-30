# SophosChat（双人版 · PRD V0.6 P0）

自部署的私人聊天软件：两个人（或小群）自由聊天，**Agent 默认不打扰**，只在被 `@` 或在与它的私聊里发言；同时 Agent 会在后台把反复聊到的事项静默整理成**按会话隔离、可追溯**的稳定主题记忆。

- 服务端：Python + FastAPI + SQLite（单进程、单文件数据库，备份就是一个文件）
- 客户端：网页 / 可安装的 PWA（电脑与手机共用一套界面，浅色/深色自动切换）
- Agent：独立的 **Bridge** 进程，常驻在你控制的设备上，模型可换（OpenAI 兼容服务 / Anthropic / 任意命令行 Agent / 演示用 mock）
- 服务器**不做任何模型推理**：语义判断（归到哪个主题、是否改口、是否被采纳）全部由外部 Agent 完成，服务器只负责存储、权限、版本与可靠提交

## 快速开始（5 分钟）

```bash
cd aichat-app
pip install -r requirements.txt            # fastapi, uvicorn, python-multipart
export PYTHONPATH=$PWD                     # 或 pip install -e .

# 1) 初始化：创建 Owner，再添加第二位使用者
python3 -m aichat.cli init --username alice --display Alice
python3 -m aichat.cli user add bob --display Bob

# 2) 启动服务（默认 127.0.0.1:8000）
python3 -m aichat.cli serve

# 3) 浏览器打开 http://127.0.0.1:8000 ，用 alice 登录
#    设置 → Agent 接入：创建凭证 → 在运行 Agent 的设备上启动 Bridge → 运行“接入自检”
python3 -m aichat_bridge --url http://127.0.0.1:8000 --token aca_xxx --adapter mock
```

想直接看效果：`bash scripts/demo.sh`（创建演示账号 alice/bob，启动服务和 mock Agent）。

> mock 适配器只是规则实现，用来联调和演示，**不是真正的智能**。真实使用请换成下面任一适配器。

### 接入真实模型

```bash
# 任意 OpenAI 兼容服务（含多数国内模型服务）
export AICHAT_LLM_API_KEY=...  AICHAT_LLM_MODEL=...  [AICHAT_LLM_BASE_URL=https://.../v1]
python3 -m aichat_bridge --url http://127.0.0.1:8000 --token $TOKEN --adapter openai

# Anthropic
export ANTHROPIC_API_KEY=...  AICHAT_LLM_MODEL=...
python3 -m aichat_bridge ... --adapter anthropic

# 本地命令行 Agent：提示词走标准输入，答案取标准输出（每次在全新的临时目录里运行）
python3 -m aichat_bridge ... --adapter command --command "codex exec -"
```

使用云端模型时，处理一条消息所需的**当前会话内容**会发送给所选服务商；不同会话分别处理，不会混在一起。

## 功能一览

**普通聊天**：双人私聊 / 群聊 / Agent 私聊；文本、Markdown、图片、文件（拖拽、粘贴上传）；回复引用、编辑、删除（真正清除正文与附件）、收藏；实时同步、断线自动补齐、发送重试不重复；未读数与未读分隔线；按关键词 / 发送者 / 日期搜索并跳到原文；日期跳转；个人归档、静音；会话级保留期限。

**Agent 互动**：`@agent`（输入 `@` 有选择器）或与 Agent 私聊触发；状态可见（等待 / 处理中 / 未及时处理 · 重试 / Agent 离线提示）；Agent 的消息永不触发它自己；任务有有效期，过期不补发过时回答。

**后台整理**：不需要 `@`；讨论静默一段时间（或达到最长等待）后，按 Room 增量批量整理；可“立即整理”、“重新整理指定范围”。

**主题记忆**（右侧面板）：稳定主题 → 条目；每条记录作者（人 / Agent）、性质（说过 / AI 建议 / 推测 / 已核实…）、状态（有分歧 / 已替代 / 待核实…）、**采纳状态**、原文出处与版本变化；用户可编辑、标记过时、删除、移动、合并主题，人工纠正优先且不会被覆盖。

**导出 / 备份**：按 Room 与日期范围导出 Markdown / JSON，可带主题记忆与变化历史、可打包附件（zip）；`aichat backup` / `restore` / `doctor`。

## 隔离与安全模型

| 机制 | 说明 |
|---|---|
| 会话隔离 | 消息、回顾、主题、条目、检索全部以 Room 为边界；服务端按任务绑定的 Room 取数，**不接受参数改写 Room** |
| 两类凭证 | 应用凭证 `aca_…` 只在 Bridge 控制层（模型看不到）；交给执行上下文的是**任务令牌**：只对一个任务 + 一个 Room + 一个用途有效，每次调用回查租约/凭证/成员关系，撤销立即生效 |
| 聊天内容 ≠ 指令 | Bridge 的提示词与 AGENT_GUIDE 都规定聊天内容只是数据；模型本身拿不到任何凭证或工具 |
| 记忆来源强校验 | 自动提取必须带 `message_id + version`；来源必须在本 Room、未删除、是当前版本，否则整批拒绝；观点/建议的作者必须是来源消息的发送者；宣称“已采纳/已拒绝”必须附用户本人的消息 |
| 原子提交 | 一批整理要么全部生效并推进游标，要么全部不生效；幂等键防重复；对象版本号防并发覆盖 |
| 删除传播 | 删除消息 = 清除正文/历史版本/附件；仅由它支撑的回顾与记忆被清理，其他条目标记待核对；只保留内容指纹防止重放再生成（不保存正文） |
| 人工纠正优先 | 手工改过的条目 `manual`，Agent 改写会得到 `manual_correction_locked` |
| Web 安全 | 密码 scrypt 哈希、登录限流、HttpOnly+SameSite Cookie、Origin 校验（含 WebSocket）、CSP、附件强制下载（伪装图片按普通文件）、Markdown 只构造 DOM 不执行 HTML |

## 部署到 VPS（Hostinger 等，Debian / Ubuntu）

> 需要的是 **VPS**（能长期运行程序、能用 root 登录）。Hostinger 的共享主机 / 网站托管不行。系统选 Ubuntu 22.04 或 24.04。在 hPanel 的防火墙里放行 **80 和 443** 端口。

```bash
# 在你自己的电脑上，把项目传到服务器（Windows 10+ 的 PowerShell 也自带 scp）
scp aichat-app.zip root@服务器IP:/root/

# 登录服务器（或用 hPanel 的“浏览器终端”），然后：
apt-get update && apt-get install -y unzip
unzip -o /root/aichat-app.zip -d /root/
bash /root/aichat-app/deploy/install.sh /root/aichat-app.zip
```

脚本会询问：管理员/第二位使用者的用户名和密码、域名。它会自动：安装依赖 → 建专用用户 → 创建账号 → 生成 Agent 凭证并写入 `/etc/aichat/bridge.env` → 注册 `aichat`、`aichat-bridge` 两个 systemd 服务（开机自启、崩溃自动重启）→ 用 Caddy 反向代理并**自动申请 HTTPS 证书** → 每天凌晨 3 点自动备份并保留 14 天。

- **域名**：有域名就先把 A 记录指向服务器 IP，安装时填入；留空会用 `<公网IP>.sslip.io`（免配置，通常可用，证书申请失败时请改用自己的域名）。
- **接入真实模型**：编辑 `/etc/aichat/bridge.env`（填 API Key、模型名，取消对应注释），再 `systemctl restart aichat-bridge`。默认是演示用的 mock。
- **升级**：上传新的 zip 后再运行同一条 `install.sh`，数据与账号保留。
- 日志：`journalctl -u aichat -f`、`journalctl -u aichat-bridge -f`。

> 说明：安装脚本在沙箱里验证了“部署代码 → 建账号 → 生成凭证与配置 → 按生成的命令启动服务与 Bridge → 登录 → Agent 在线 → 重复运行升级 → 备份命令”这条链路；**apt 安装、创建系统用户、systemd 注册、Caddy 与 HTTPS 证书这几步依赖真实的 Debian/Ubuntu 服务器，没有在真机上跑过**，首次运行请留意终端输出。

## 部署注意

- **只运行一个 worker**（实时推送在进程内）；双人/小群完全够用。
- 公网使用请放在 HTTPS 反向代理之后，并设置 `AICHAT_COOKIE_SECURE=1`。Caddy 示例（未在本环境验证）：`chat.example.com { reverse_proxy 127.0.0.1:8000 }`
- 数据都在 `AICHAT_DATA_DIR`（默认 `./data`）：`aichat.db`、`attachments/`、`secret.key`。**备份文件包含签名密钥与凭证哈希，请妥善保管。**
- Bridge 设备关机时，聊天照常保存；重新启动后会补整理，互动请求按有效期处理（默认 10 分钟）。

常用环境变量：`AICHAT_CHAT_TASK_TTL`（互动有效期，秒）、`AICHAT_CONSOLIDATE_QUIET`（静默多久开始整理，默认 300）、`AICHAT_CONSOLIDATE_MAX_WAIT`（最长等待，默认 3600）、`AICHAT_MAX_UPLOAD_MB`、`AICHAT_AGENT_OFFLINE_AFTER`。

## 目录

```
aichat/            服务端（api.py 浏览器接口 · agent_api.py 工具接口 · tasks.py 任务队列 · memory.py 记忆 · chat.py 聊天）
aichat/static/     网页客户端（原生 ES 模块，无构建步骤）
aichat/AGENT_GUIDE.md   给 Agent 的规范（服务器维护唯一可信版本，/agent-guide.md）
aichat_bridge/     Agent Bridge（仅依赖标准库）与适配器
tests/             67 个自动化测试     scripts/e2e.py  浏览器端到端（Playwright）
```

## 验证情况

- `pytest tests`：67 项通过（聊天、鉴权、越权、任务租约、记忆提交、失效传播、导出备份、Bridge 集成含真实服务与 WebSocket）。
- `python3 scripts/e2e.py`：两个浏览器 + 手机视口 + 真实服务 + 真实 Bridge：实时聊天、`@` 触发、Agent 私聊、编辑/回复/收藏、附件与图片、后台整理→主题记忆→人工纠正→跳转原文、Agent 接入自检、搜索、导出、未读、断线重连补齐、离线发送自动重试、Agent 离线→恢复、深色模式。
- 规模自测：单个会话 10 万条消息时，翻页 < 1 ms，全文搜索 ≈ 25 ms，会话列表 ≈ 16 ms。

## 与 PRD 的对照（诚实版）

| PRD 要求 | 状态 |
|---|---|
| 普通聊天、历史、搜索、归档、导出、备份恢复 | ✅ 已实现并测试 |
| 默认 `@`/私聊触发、AI 状态可见、过期与重试幂等 | ✅ |
| 后台整理（独立游标、原子提交、失败补处理）、稳定主题、新增/修订/替代/分歧/采纳、AI 内容入库 | ✅ 平台侧完整；**归并质量取决于所接模型，尚未用真实中文聊天样本评测**（PRD §12.3 的评测需要你的样本，本仓库未包含评测脚本） |
| 会话隔离、任务作用域令牌、凭证撤销、删除与纠正传播 | ✅ |
| 一个常驻任务 + 按 Room 隔离执行 | ✅ Bridge 任务间不共享状态；command 适配器每次在新临时目录运行 |
| 接入向导（说明→运行位置→Agent→凭证→规范→启动→自检） | ✅ 自检覆盖“连通 + 领取任务 + 回复”；**不含测试记忆写回** |
| HTTP API + 机器可读 schema（`/openapi.json`）+ 版本化 AGENT_GUIDE | ✅ |
| 管理 CLI | ✅ `aichat`；**没有聊天/记忆工具类 CLI** |
| **MCP Server** | ❌ 未实现（Bridge 直接走 HTTP，由 Bridge 编排模型，模型不直接调用工具） |
| Codex SDK 适配 | ❌ 未实现；提供通用 `command` 适配器，**未在真实 Codex 上验证** |
| openai / anthropic 适配器 | ⚠️ 已实现，**因沙箱无密钥与外网，未对真实服务验证**；解析与重试逻辑由 mock/回调适配器测试覆盖 |
| 手机收消息与通知 | ⚠️ 响应式 + 可安装 PWA；**网页关闭后没有后台推送**（需要 Web Push/原生推送，国内安卓尤其受限），目前只有页面开着时的桌面通知 |
| P1 跨会话授权披露 | ❌ 未启用（接口返回 `not_enabled`，Agent 不能模拟批准） |
| 语音消息 | ❌（P1） |

已知取舍：搜索用 `LIKE`（中文子串匹配，十万级消息足够；更大规模再换 FTS/trigram）；整理批次把“已有主题”整体放进提示词（双人场景足够，主题很多时需要先做候选检索）；实时推送为进程内单实例。

## 0.2 新增

- 输入框：相册 / 文件分开的入口、表情面板、输入为空时右侧按钮变成录音键
- 语音消息（点一下录音，再点发送；点垃圾桶取消；单条最长 5 分钟）
- 消息表情回应、对方“正在输入”提示
- 登录设备管理：设置 → 账号与通知，可单独退出某台设备
- 离线推送（Web Push）：依赖 `pywebpush`；设置里打开“离线推送”开关即可。VAPID 密钥自动生成在数据目录（`vapid_private.pem`），备份时请一并保留。
  环境变量 `AICHAT_VAPID_SUBJECT` 可设置联系邮箱（默认 `mailto:admin@example.com`）。
- 数据库自动升级（只加列/加表，不改旧数据）

## 通话（语音 / 视频）

一对一私聊的顶部有电话 / 摄像头按钮。信令走本服务的 WebSocket，音视频由浏览器直连（WebRTC）。
两端都在普通家庭/移动网络时，直连常常失败，需要一台 TURN 中继（推荐 coturn，`use-auth-secret` 模式）。配置（写进 `/etc/aichat/server.env`，重启 `aichat`）：

    AICHAT_TURN_URL=turn:你的域名:3478?transport=udp,turns:你的域名:5349?transport=tcp
    AICHAT_TURN_SECRET=与 coturn 的 static-auth-secret 相同
    # 可选：自定义 STUN（默认谷歌公共 STUN，国内不可用）
    AICHAT_ICE_SERVERS=[{"urls":"stun:你的域名:3478"}]

服务器会给每位用户发放 6 小时有效的临时 TURN 账号。未配置 TURN 时，同一局域网或双方都有公网可达的网络下仍可能通话。

## 联网搜索（可选）

模型本身不会上网。Bridge 可以给聊天回复加一个 `web_search` 工具，由模型自己决定什么时候搜（整理记忆的任务不联网）。在 `/etc/aichat/bridge.env` 里配置其中一种后重启 `aichat-bridge`：

    AICHAT_SEARCH_PROVIDER=tavily        # 或 brave / searxng
    AICHAT_SEARCH_API_KEY=你的 Key       # tavily / brave 需要
    AICHAT_SEARCH_URL=http://127.0.0.1:8888   # 仅 searxng（自建）需要

搜索词会发给所选的搜索服务；搜索结果被当作不可信内容交给模型。模型服务不支持函数调用时会自动退回普通回复。目前仅 `openai` 适配器支持。

如果你的模型服务支持 `/v1/responses` 的内置 `web_search` 工具（OpenAI 官方、部分中转服务），可以不申请搜索 Key，直接在 `bridge.env` 里加一行 `AICHAT_NATIVE_SEARCH=1`；不支持时会自动退回到上面自带的搜索工具。
