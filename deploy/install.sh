#!/usr/bin/env bash
# SophosChat 一键安装 / 升级（Debian / Ubuntu VPS，用 root 运行）
#   用法：bash install.sh /root/aichat-app.zip
# 可重复运行：再次运行并传入新的 zip 即为升级（数据与账号保留）。
# 非交互：OWNER_USER OWNER_PASS USER2 USER2_PASS DOMAIN 用环境变量传入；NO_HTTPS=1 只开 8000 端口的 HTTP（仅供临时测试）。
# 与现有服务共存（服务器上已有 nginx/xray 占用 80/443 时）：
#   PROXY=own-nginx PUBLIC_HOST=主机名 PUBLIC_PORT=8080 TLS_CERT=fullchain.pem TLS_KEY=privkey.pem APP_PORT=8765 SKIP_UFW=1
#   → 应用只监听 127.0.0.1:APP_PORT，用独立的 nginx 进程在 PUBLIC_PORT 提供 HTTPS；不安装 Caddy，不改现有 nginx 与防火墙。
set -euo pipefail

ZIP="${1:-}"
PREFIX="${ROOT_PREFIX:-}"            # 仅测试用：把所有路径挪到某个前缀下
TEST="${INSTALL_TEST:-}"             # 仅测试用：跳过 apt / 用户 / systemd / caddy
APP_DIR="$PREFIX/opt/aichat"
DATA_DIR="$PREFIX/var/lib/aichat"
ETC_DIR="$PREFIX/etc/aichat"
UNIT_DIR="$PREFIX/etc/systemd/system"
BACKUP_DIR="$PREFIX/var/backups/aichat"
PORT="${APP_PORT:-8000}"
PROXY="${PROXY:-caddy}"          # caddy | own-nginx | none
PUBLIC_HOST="${PUBLIC_HOST:-}"
NGINX_BIN="$(command -v nginx 2>/dev/null || echo /usr/sbin/nginx)"
PUBLIC_PORT="${PUBLIC_PORT:-8080}"
TLS_CERT="${TLS_CERT:-}"
TLS_KEY="${TLS_KEY:-}"
SKIP_UFW="${SKIP_UFW:-}"
DOMAIN="${DOMAIN:-}"
USER2="${USER2:-}"
USER2_PASS="${USER2_PASS:-}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { echo "错误：$*" >&2; exit 1; }
as_app() { if [ -n "$TEST" ]; then "$@"; else runuser -u aichat -- "$@"; fi; }

[ -n "$TEST" ] || [ "$(id -u)" -eq 0 ] || die "请用 root 运行（先 sudo -i）"
[ -n "$ZIP" ] && [ -f "$ZIP" ] || die "用法：bash install.sh /路径/aichat-app.zip"
if [ -z "$TEST" ]; then
  command -v apt-get >/dev/null || die "目前只支持 Debian / Ubuntu（apt）。你的系统不是的话，把系统版本告诉我，我再改"
fi

# ------------------------------------------------------------------ 参数
read_default() { # 变量名 提示 默认值
  local var="$1" prompt="$2" def="$3" val=""
  if [ -n "${!var:-}" ]; then return; fi
  if [ -t 0 ]; then read -r -p "$prompt${def:+ [$def]}：" val || true; fi
  printf -v "$var" '%s' "${val:-$def}"
}
read_secret() { # 变量名 提示
  local var="$1" prompt="$2" a="" b=""
  if [ -n "${!var:-}" ]; then return; fi
  [ -t 0 ] || die "非交互运行时请通过环境变量 $var 传入"
  while true; do
    read -r -s -p "$prompt（至少 8 位）：" a; echo
    read -r -s -p "再输入一次：" b; echo
    [ "$a" = "$b" ] && [ "${#a}" -ge 8 ] && break
    echo "两次不一致或少于 8 位，请重试"
  done
  printf -v "$var" '%s' "$a"
}

say "基本信息"
read_default OWNER_USER "管理员（Owner）用户名，字母数字下划线" "alice"
read_secret  OWNER_PASS "管理员密码"
read_default USER2 "第二位使用者用户名（留空则暂不创建）" "bob"
if [ -n "$USER2" ]; then read_secret USER2_PASS "$USER2 的初始密码"; fi
if [ -z "${NO_HTTPS:-}" ] && [ "$PROXY" = "caddy" ]; then
  read_default DOMAIN "访问用的域名（A 记录需已指向本服务器；留空则自动使用 <公网IP>.sslip.io）" ""
  if [ -z "$DOMAIN" ]; then
    IP="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
    [ -n "$IP" ] || die "取不到公网 IP，请用 DOMAIN=你的域名 重新运行"
    DOMAIN="${IP}.sslip.io"
  fi
fi

# ------------------------------------------------------------------ 系统依赖
if [ -z "$TEST" ]; then
  say "安装系统依赖"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip unzip curl ca-certificates
  if [ "$PROXY" = "own-nginx" ] && ! command -v nginx >/dev/null; then die "own-nginx 模式需要服务器上已有 nginx（apt install nginx 会顺带启动一个占用 80 端口的默认站点，所以脚本不替你装）"; fi
  id aichat >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin aichat
fi
mkdir -p "$APP_DIR" "$DATA_DIR" "$ETC_DIR" "$BACKUP_DIR" "$UNIT_DIR"

# ------------------------------------------------------------------ 代码
say "部署代码到 $APP_DIR"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -d "$TMP"
SRC="$(find "$TMP" -maxdepth 2 -name pyproject.toml -printf '%h\n' | head -1)"
[ -n "$SRC" ] || die "zip 里没有找到 aichat-app 项目"
mkdir -p "$APP_DIR/app"
if command -v rsync >/dev/null; then rsync -a --delete --exclude data --exclude data-demo "$SRC"/ "$APP_DIR/app"/
else rm -rf "$APP_DIR/app"; mkdir -p "$APP_DIR/app"; cp -a "$SRC"/. "$APP_DIR/app"/; fi
if [ ! -x "$APP_DIR/venv/bin/python" ]; then python3 -m venv "$APP_DIR/venv"; fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/app/requirements.txt"
if [ -z "$TEST" ]; then
  chown -R root:root "$APP_DIR"
  chown aichat:aichat "$DATA_DIR" "$BACKUP_DIR"
  chmod 700 "$DATA_DIR" "$BACKUP_DIR"
fi

# ------------------------------------------------------------------ 配置文件
COOKIE_SECURE=1; HOST=127.0.0.1
if [ -n "${NO_HTTPS:-}" ] || [ "$PROXY" = "none" ]; then COOKIE_SECURE=0; fi
if [ -n "${NO_HTTPS:-}" ] && [ "$PROXY" = "caddy" ]; then HOST=0.0.0.0; fi
if [ ! -f "$ETC_DIR/server.env" ]; then
  cat > "$ETC_DIR/server.env" <<ENV
AICHAT_DATA_DIR=$DATA_DIR
AICHAT_COOKIE_SECURE=$COOKIE_SECURE
# 互动请求有效期（秒）、讨论静默多久后开始整理、最长等待（秒）
AICHAT_CHAT_TASK_TTL=600
AICHAT_CONSOLIDATE_QUIET=300
AICHAT_CONSOLIDATE_MAX_WAIT=3600
ENV
fi
CLI=(env "PYTHONPATH=$APP_DIR/app" "AICHAT_DATA_DIR=$DATA_DIR" "$APP_DIR/venv/bin/python" -m aichat.cli)

# ------------------------------------------------------------------ 账号
say "创建账号"
if as_app "${CLI[@]}" user list 2>/dev/null | grep -q " owner"; then
  echo "已存在 Owner，跳过账号创建（升级模式）"
else
  as_app env AICHAT_PASSWORD="$OWNER_PASS" "${CLI[@]}" init --username "$OWNER_USER" --display "$OWNER_USER" >/dev/null
  echo "已创建 Owner：$OWNER_USER"
  if [ -n "$USER2" ]; then
    as_app env AICHAT_PASSWORD="$USER2_PASS" "${CLI[@]}" user add "$USER2" --display "$USER2" >/dev/null
    echo "已创建用户：$USER2"
  fi
fi

# ------------------------------------------------------------------ Agent Bridge 配置（凭证自动生成并写入，不需要你复制）
if [ ! -f "$ETC_DIR/bridge.env" ]; then
  TOKEN="$(as_app "${CLI[@]}" agent token --label "server-bridge" | grep -o 'aca_[^ ]*' | head -1)"
  [ -n "$TOKEN" ] || die "生成 Agent 凭证失败"
  umask 077
  cat > "$ETC_DIR/bridge.env" <<ENV
AICHAT_URL=http://127.0.0.1:$PORT
AICHAT_AGENT_TOKEN=$TOKEN
# 当前是演示用的 mock（规则实现，不是真正的模型）。接入真实模型：改下面三行，然后 systemctl restart aichat-bridge
AICHAT_ADAPTER=mock
#AICHAT_ADAPTER=openai
#AICHAT_LLM_BASE_URL=https://api.openai.com/v1
#AICHAT_LLM_API_KEY=
#AICHAT_LLM_MODEL=
#AICHAT_ADAPTER=anthropic
#ANTHROPIC_API_KEY=
ENV
  umask 022
fi
if [ -z "$TEST" ]; then chown root:aichat "$ETC_DIR/bridge.env" "$ETC_DIR/server.env"; chmod 640 "$ETC_DIR/bridge.env" "$ETC_DIR/server.env"; fi

# ------------------------------------------------------------------ systemd
say "写入服务"
cat > "$UNIT_DIR/aichat.service" <<UNIT
[Unit]
Description=SophosChat 服务
After=network-online.target
Wants=network-online.target

[Service]
User=aichat
Group=aichat
WorkingDirectory=$APP_DIR/app
EnvironmentFile=$ETC_DIR/server.env
Environment=PYTHONPATH=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m aichat.cli serve --host $HOST --port $PORT
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
cat > "$UNIT_DIR/aichat-bridge.service" <<UNIT
[Unit]
Description=SophosChat Agent Bridge
After=aichat.service
Wants=aichat.service

[Service]
User=aichat
Group=aichat
WorkingDirectory=$APP_DIR/app
EnvironmentFile=$ETC_DIR/bridge.env
Environment=PYTHONPATH=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/python -m aichat_bridge --guide-path $DATA_DIR/AGENT_GUIDE.md
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT

if [ "$PROXY" = "own-nginx" ]; then
  [ -n "$PUBLIC_HOST" ] && [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ] || die "own-nginx 模式需要 PUBLIC_HOST、TLS_CERT、TLS_KEY"
  cat > "$ETC_DIR/nginx.conf" <<NGINX
# SophosChat 专用的独立 nginx 进程：不读取、不修改系统 nginx 的任何配置
pid /run/aichat-nginx/nginx.pid;
error_log /var/log/aichat-nginx.log warn;
worker_processes 1;
events { worker_connections 512; }
http {
    access_log off;
    client_body_temp_path /run/aichat-nginx/body;
    proxy_temp_path /run/aichat-nginx/proxy;
    fastcgi_temp_path /run/aichat-nginx/fastcgi;
    uwsgi_temp_path /run/aichat-nginx/uwsgi;
    scgi_temp_path /run/aichat-nginx/scgi;
    map \$http_upgrade \$connection_upgrade { default upgrade; '' close; }
    server {
        listen $PUBLIC_PORT ssl;
        server_name $PUBLIC_HOST;
        ssl_certificate $TLS_CERT;
        ssl_certificate_key $TLS_KEY;
        ssl_protocols TLSv1.2 TLSv1.3;
        client_max_body_size 30m;
        location / {
            proxy_pass http://127.0.0.1:$PORT;
            proxy_http_version 1.1;
            proxy_set_header Host \$http_host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }
    }
}
NGINX
  cat > "$UNIT_DIR/aichat-proxy.service" <<UNIT
[Unit]
Description=SophosChat HTTPS 入口（独立 nginx）
After=network-online.target aichat.service

[Service]
RuntimeDirectory=aichat-nginx
ExecStartPre=$NGINX_BIN -t -c $ETC_DIR/nginx.conf
ExecStart=$NGINX_BIN -g 'daemon off;' -c $ETC_DIR/nginx.conf
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  # 证书续期后让本入口重新加载（新增一个独立的 hook 文件，不改动已有的 hook）
  mkdir -p "$PREFIX/etc/letsencrypt/renewal-hooks/deploy"
  cat > "$PREFIX/etc/letsencrypt/renewal-hooks/deploy/aichat-proxy-reload" <<'HOOK'
#!/bin/sh
systemctl reload aichat-proxy 2>/dev/null || true
HOOK
  chmod 755 "$PREFIX/etc/letsencrypt/renewal-hooks/deploy/aichat-proxy-reload"
fi

# 每天凌晨 3 点备份，保留 14 天（备份文件含密钥，目录仅 aichat 用户可读）
cat > "$PREFIX/etc/cron.d-aichat-backup" <<CRON
0 3 * * * aichat umask 077; env PYTHONPATH=$APP_DIR/app AICHAT_DATA_DIR=$DATA_DIR $APP_DIR/venv/bin/python -m aichat.cli backup $BACKUP_DIR/aichat-\$(date +\%F).tar.gz >/dev/null 2>&1; find $BACKUP_DIR -name 'aichat-*.tar.gz' -mtime +14 -delete
CRON

if [ -z "$TEST" ]; then
  mv "$PREFIX/etc/cron.d-aichat-backup" /etc/cron.d/aichat-backup
  chmod 644 /etc/cron.d/aichat-backup

  if [ -z "${NO_HTTPS:-}" ] && [ "$PROXY" = "caddy" ]; then
    say "安装 Caddy（自动申请并续期 HTTPS 证书）"
    if ! command -v caddy >/dev/null; then
      apt-get install -y -qq caddy || {
        apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https gnupg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update -qq && apt-get install -y -qq caddy
      }
    fi
    cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:$PORT
}
CADDY
    systemctl enable caddy >/dev/null 2>&1; systemctl restart caddy
  fi

  if [ -z "$SKIP_UFW" ] && command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    case "$PROXY" in
      caddy) ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; [ -z "${NO_HTTPS:-}" ] || ufw allow $PORT/tcp >/dev/null ;;
      own-nginx) ufw status | grep -qE "^$PUBLIC_PORT(/tcp)? +ALLOW" || ufw allow $PUBLIC_PORT/tcp >/dev/null ;;
    esac
  fi

  say "启动服务"
  systemctl daemon-reload
  systemctl enable aichat aichat-bridge >/dev/null 2>&1
  systemctl restart aichat
  for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null || { journalctl -u aichat -n 30 --no-pager; die "服务没有正常启动，日志见上"; }
  systemctl restart aichat-bridge
  if [ "$PROXY" = "own-nginx" ]; then systemctl enable aichat-proxy >/dev/null 2>&1; systemctl restart aichat-proxy; sleep 1; systemctl is-active --quiet aichat-proxy || { journalctl -u aichat-proxy -n 20 --no-pager; die "HTTPS 入口没有启动，日志见上"; }; fi
fi

# ------------------------------------------------------------------ 完成
if [ "$PROXY" = "own-nginx" ]; then URL="https://$PUBLIC_HOST:$PUBLIC_PORT"; elif [ -n "${NO_HTTPS:-}" ] || [ "$PROXY" = "none" ]; then URL="http://<服务器IP>:$PORT"; else URL="https://$DOMAIN"; fi
cat <<DONE

====================================================================
 安装完成
 访问地址：$URL
 管理员：$OWNER_USER ${USER2:+     第二位使用者：$USER2}
 数据目录：$DATA_DIR      每日备份：$BACKUP_DIR（保留 14 天）
--------------------------------------------------------------------
 下一步：
  1) 浏览器打开上面的地址登录；
  2) 要接入真实模型：编辑 $ETC_DIR/bridge.env（填 API Key 和模型名，取消对应注释），
     然后执行：systemctl restart aichat-bridge
  3) 在网页「设置 → Agent 接入」点“运行接入自检”
 常用命令：
  查看日志   journalctl -u aichat -f      journalctl -u aichat-bridge -f
  重启       systemctl restart aichat aichat-bridge
  升级       上传新 zip 后再运行：bash install.sh /root/新的aichat-app.zip
  手动备份   sudo -u aichat env PYTHONPATH=$APP_DIR/app AICHAT_DATA_DIR=$DATA_DIR $APP_DIR/venv/bin/python -m aichat.cli backup /var/backups/aichat/manual.tar.gz
====================================================================
DONE
