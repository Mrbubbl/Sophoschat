# 部署、升级与备份

适用版本：0.6.0。聊天 Agent 的语言模型通过 Anthropic 协议 API 调用；语音识别在本服务器 CPU 上运行，需要单独安装权重。两者不需要本地 GPU。语音依赖、模型安装、配置和恢复见 [ASR 部署说明](ASR.md)。

## 本地启动

需要 Python 3.10+、Node.js/npm。以下命令适用于 macOS/Linux；首次初始化会交互要求输入密码，仓库不包含默认账号密码。

```bash
git clone https://github.com/liuchenyan29-rgb/Sophoschat.git
cd Sophoschat
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[browser]'
python -m playwright install chromium
# Linux 缺少浏览器系统依赖时：python -m playwright install-deps chromium

python -m aichat.cli init --username owner --display Owner
# 可选；也可在网页注册或通过 Owner 添加成员
python -m aichat.cli user add alice --display Alice
python -m aichat.cli serve --host 127.0.0.1 --port 8000
```

仓库已公开，可直接克隆；推送代码仍需自己的 GitHub 认证和权限。浏览器打开 `http://127.0.0.1:8000`，使用刚创建的账号登录。启动应用时自动启动 SDK 控制器，无需另开 Bridge。

## 配置模型与工具

Owner 打开「设置 → Agent 接入」，填写模型 ID、API Key、可选网关地址，保存并启用 AI，再点击真实 SDK 自检。网关必须支持 SDK 所需的 Anthropic Messages 协议；模型列表可访问不代表工具调用一定兼容。

「设置 → 工具与 MCP」配置搜索服务、外部 MCP 和注册开关。API Key 与 MCP 凭证存储在服务端加密配置中，密钥字段不回传页面。操作说明见 [使用指南](USAGE.md)。

默认开放自主注册，所有 Agent 都启用完整工具和自动执行权限。实际操作系统权限等于服务账号，应用不自动赋予 root/sudo；完整终端不提供系统级的 Room 隔离。部署者应按实际使用者范围决定注册是否开放。

## 运行配置

通过进程环境设置以下项；程序不自动加载 `.env` 文件。模型与 MCP 凭证通过 Owner 页面配置。

| 配置 | 默认值 | 用途 |
|---|---|---|
| `AICHAT_DATA_DIR` | `./data` | 数据库、附件、交付物、密钥及 SDK 状态 |
| `AICHAT_COOKIE_SECURE` | `0` | HTTPS 公网部署设置为 `1` |
| `AICHAT_AGENT_BASE_URL` | `http://127.0.0.1:8000` | SDK 控制器回连应用的地址 |
| `AICHAT_CHROMIUM_PATH` | Playwright 默认查找 | 指定已安装 Chromium 的可执行文件 |
| `AICHAT_START_AGENT_SERVICE` | `1` | 应用是否托管 SDK 控制器 |
| `AICHAT_ALLOW_LEGACY_AGENT` | `0` | 仅兼容测试/演示使用旧 Bridge |
| `AICHAT_CHAT_TASK_TTL` | `600` | 互动请求有效期，秒 |
| `AICHAT_LEASE_SECONDS` | `120` | 任务租约，执行期间自动续租 |
| `AICHAT_CONSOLIDATE_QUIET` | `300` | 后台整理等待讨论静默，秒 |
| `AICHAT_CONSOLIDATE_MAX_WAIT` | `3600` | 后台整理最长等待，秒 |
| `AICHAT_VAPID_SUBJECT` | 示例邮箱 | Web Push 联系标识；部署时改为真实联系邮箱或 URL |

使用 CLI `serve --port` 时会自动同步 SDK 回连端口。自建 ASGI 启动器必须设置正确的 `AICHAT_AGENT_BASE_URL`，并只运行一个应用 worker。

## Linux 持续运行

`deploy/install.sh` 面向 Debian/Ubuntu，以 root 安装系统依赖和 systemd 单元，应用默认使用 `aichat` 账号。部署前阅读脚本参数；它会管理所选的反向代理服务。

```bash
# 从明确的分支或提交打包；上传修改分支时请指定该分支
git archive --format=zip --prefix=aichat-app/ \
  --output=/tmp/sophoschat.zip HEAD
sudo bash deploy/install.sh /tmp/sophoschat.zip
```

公网应使用 HTTPS，应用监听回环地址，由反向代理提供入口，并设置 `AICHAT_COOKIE_SECURE=1`。手机不能访问开发电脑的 `127.0.0.1`；需要它能连接到的 HTTPS 域名。

Chromium 要安装到运行账号可访问的位置。安装脚本不会自动完成浏览器下载：

```bash
sudo /opt/aichat/venv/bin/python -m playwright install-deps chromium
sudo -u aichat env PLAYWRIGHT_BROWSERS_PATH=/var/lib/aichat/browsers \
  /opt/aichat/venv/bin/python -m playwright install chromium

# 获取该安装位置的实际可执行文件路径
sudo -u aichat env PLAYWRIGHT_BROWSERS_PATH=/var/lib/aichat/browsers \
  /opt/aichat/venv/bin/python -c \
  'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()'
```

将输出以 `AICHAT_CHROMIUM_PATH=...` 写入 `/etc/aichat/server.env`，再重启 `aichat`。不要复制开发电脑的绝对路径。默认安装到 root 的浏览器缓存通常不适合服务账号读取。

```bash
sudo systemctl restart aichat
sudo journalctl -u aichat -n 100 --no-pager
curl -f http://127.0.0.1:8000/api/health
```

自定义了端口时替换健康检查地址。健康检查成功只证明应用运行；模型、工具和文件存储以网页真实自检结果为准。

## 升级与回滚

0.6.0 将数据库升级到 schema 10，增加个人联系人表和空私聊的个人显示状态。注册入口、开关及邀请链接保持原规则；新建账号仅初始化 Agent 私聊，联系人需手动添加。

升级前使用旧程序备份，在部署新代码后、第一次启动前可运行只读预览（不要先执行会打开并迁移数据库的其他新 CLI 命令）：

```bash
python -m aichat.cli --data-dir /var/lib/aichat contacts-migration --preview
```

输出待归档的旧空双人 Room 和人类成员关系数量。首次正常启动或其他打开数据库的新 CLI 命令会自动执行一次迁移，并在 `kv.contacts_migration_v10` 保存时间和统计。预览不会执行迁移；迁移完成后再次预览显示先前记录。

迁移仅归档没有任何消息（包括删除痕迹）、任务或交付物的旧双人私聊，保留 Room 和历史数据；群聊、Agent 私聊和有内容的双人会话不变，不自动生成联系人。可以从「已归档」恢复空会话；联系人关系随数据库备份恢复。新静态资源需刷新，Service Worker 缓存版本已更新。

升级前用旧版本创建完整备份。新版本会自动做追加式数据库迁移。旧 Bridge 在安装脚本升级时停用；原账号、密码、聊天和附件保留。

`deploy/update.sh` 支持通过部署密钥拉取指定分支/提交，但包含既有部署参数，使用前必须检查 `/etc/aichat/update.env`、域名、代理和端口。不要把它原有的站点默认值用于新服务器。新修改尚在 PR 分支时，不能假定 `main` 已包含这些改动。

回滚代码后不应假定旧程序兼容已迁移的新数据库；需要回滚数据时恢复升级前备份。恢复和升级会影响运行中的任务，应安排维护时间。

## 备份

```bash
python -m aichat.cli backup /safe/location/sophoschat.tar.gz
# 恢复到新的空目录，先核查再替换生产目录
python -m aichat.cli --data-dir ./restored restore /safe/location/sophoschat.tar.gz
python -m aichat.cli --data-dir ./restored doctor
```

完整备份包含数据库、附件、交付文件、加密配置的解密密钥、SDK 会话和项目工作区。备份属于私人数据，不应提交 Git。仅复制数据库不足以完整恢复文件和凭证。
