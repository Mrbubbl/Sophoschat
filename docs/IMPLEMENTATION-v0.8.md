# V0.8 实施与验收记录

日期：2026-11-02。开发分支为 `codex/server-sdk-artifacts`；仓库与 PR 的发布状态以当前 GitHub 记录为准。

## 完整执行环境

所有 chat / delivery / memory 执行均使用 Claude Agent SDK 的完整 `claude_code` 工具预设、`allowed_tools=["*"]`、`permission_mode="bypassPermissions"`，关闭 SDK 命令沙箱。可以使用 Bash、读写文件、执行 Python、npm、安装依赖、测试和构建。SDK 内部 Agent 也使用该工具环境，不增加单独审批。

系统权限等于部署服务账号，本应用不自动创建 root/sudo 权限。工作目录独立于 SDK 会话目录，保存在 `data/agent_state/workspaces/room-<id>`，配置变更和新任务不清空文件。服务账号可访问的文件不受 Room API 校验隔离，因此本模式不承诺操作系统级的跨会话隔离。费用、时间、轮次、取消和进程回收机制保留。

PATH 包含项目 Python 环境、系统及常见 Node/npm 安装目录；不把个人终端标志、代理或其他环境里的云密钥自动传给模型。需要的外部工具环境变量通过 Owner 的 MCP 配置显式提供。Linux 安装脚本增加 Node/npm，并移除旧 `ProtectSystem/ProtectHome/NoNewPrivileges` 服务限制；仍使用原服务账号运行。

## 联网搜索与通用浏览器

平台提供 `internet_search`、`read_webpage`、`browser`，全部用途可用。默认搜索先用 Bing RSS，失败或无结果时尝试 DuckDuckGo；没有把聊天内部检索冒充联网。支持切换 Brave、Tavily、SearXNG。无 Key 的公共入口遇到挑战/限流会明确失败，不伪造结果。

HTTP 读取支持跳转、正文/链接提取与响应大小限制。Playwright Chromium 支持 goto、inspect、click、fill、press、scroll、back、evaluate 和 screenshot；浏览器开启网络，状态在当前 Room 工作目录保存。网页内容仍作为资料，成功依据为真实工具回执。

## 外部 MCP

设置新增「工具与 MCP」页，支持 stdio、HTTP、SSE 配置、保存、删除及独立连接测试。HTTP 服务可以配置认证 headers，stdio 可以配置 command/args/env。配置整体加密保存，headers/env/args 在页面以 `__UNCHANGED__` 表示，编辑保存可保留原值。应用 API Key 不因此回传前端。

配置变更会提升执行配置版本并使旧任务租约失效；新 SDK 执行原生挂载全部已配置 MCP 服务。连接测试使用真实 MCP initialize 和 tools/list，在独立进程执行并设置超时。SDK 工具调用名称按任务记录，参数/凭证不写入该记录；聊天进度显示执行代码、搜索、浏览或外部调用。

日历、云盘等服务需要其 MCP 地址和服务认证，本轮未虚构或自动连接用户的真实第三方账号；也未实现各服务专属 OAuth 登录页面。服务端可使用已提供的静态 Token / Bearer 等认证。测试使用独立 notes MCP 服务，真实写入并读回数据，不冒充已操作真实日历或云盘。

## 账号注册

登录页支持切换注册；用户名、昵称、密码、确认密码及错误提示完整。注册后自动登录并建立 Agent 私聊及聊天入口；普通注册固定 user 角色，不能注入 Owner 权限。包含来源、用户名、重名、密码和频率校验，Cookie 延续现有 HttpOnly/SameSite 策略。

默认开放注册，Owner 可在工具页关闭。关闭后登录页隐藏入口，直接调用注册接口也会拒绝。旧账号、密码及记录保留。

新增接口：`GET /api/registration` 公开查询，`POST /api/register` 注册；Owner 使用 `PUT /api/registration` 切换。工具配置为 `GET/PUT /api/agent/integrations`，握手测试为 `POST /api/agent/integrations/check`。配置与测试接口只允许 Owner。

## 验证

- 完整回归 **138 项通过**。新增注册/角色注入/关闭注册/频率限制、配置加密与保留、真实 stdio MCP 写入、真实 HTTP MCP 认证握手、网页解析及执行事件租约校验。
- 使用用户指定 `https://api.daitu.cc` 的 `claude-sonnet-4-6` 完成真实 SDK 测试：Bash 创建 Python 测试并通过，npm 安装 `is-number`、运行 npm test 与 npm run build，真实生成 `dist/result.txt` 的 `BUILD_OK`。
- 同一次真实 SDK 执行实际调用 `internet_search`、`browser`、外部 `append_note` 和 `list_notes`；搜索结果、Example Domain 网页标题及 `V8_EXTERNAL_MCP_OK` 数据读回均有回执。
- 这次综合执行 SDK 费用估算约 $0.307。Haiku 在此前尝试返回连续 502，已停止该尝试；中断费用记为未知并保留预算，未将其视为成功。SDK 估算不等同网关实际账单。
- 独立互联网验证通过：搜索结果、HTTP 网页读取、Chromium 打开真实网页、执行 JavaScript、截图。
- 真浏览器验收通过：注册密码不一致提示、注册与自动登录、普通成员角色、MCP 配置显示/保留、真实连接测试、注册开关。
- 最新版服务的真实 SDK 自检已通过（包含工具调用事件、文件持久化与读回）。最终服务仍在 `http://127.0.0.1:8000` 运行；原 Owner 可登录，新版本注册已开放。默认模型已切换为本轮完整工具测试通过的 Sonnet。

日志、截图和不含密钥的结构化结果位于 `output/validation-v8/`。最终源码及交付报告扫描未发现用户模型 Key；真实测试配置只在独立数据目录和本地服务数据中加密保存。

后续范围：专用 GitHub 操作产品流程、多文件/二进制交付卡片、Word/PDF/Excel/PPT/图片等专门的制作解析工具；本轮未扩展这些交付接口。
