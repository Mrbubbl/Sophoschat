#!/usr/bin/env bash
# SophosChat 在线更新：从 GitHub 拉取最新代码 → 备份数据 → 升级安装 → 健康检查
#
# 首次使用（只需一次，root 运行）：
#   bash update.sh --setup git@github.com:Mrbubbl/Sophoschat.git
#     生成只读 deploy key，打印公钥；把公钥加到 GitHub 仓库 Settings → Deploy keys（不要勾选 write）
#   之后：bash /opt/aichat/src/deploy/update.sh        # 更新到 main 最新
#         bash /opt/aichat/src/deploy/update.sh v1.2   # 更新到指定分支/标签/提交
#
# 回滚：数据备份在 /var/backups/aichat-pre-<commit>-<时间>；代码回滚只需用旧提交再运行本脚本。
set -Eeuo pipefail

CONF=/etc/aichat/update.env
SRC=/opt/aichat/src
KEY=/etc/aichat/deploy_key
LOG=/var/log/aichat-update.log

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m失败：%s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "请用 root 运行"

# ---------------------------------------------------------------- 首次设置
if [ "${1:-}" = "--setup" ]; then
  REPO="${2:-}"; [ -n "$REPO" ] || die "用法：update.sh --setup git@github.com:用户/仓库.git"
  command -v git >/dev/null || apt-get install -y git >/dev/null
  command -v zip >/dev/null || apt-get install -y zip >/dev/null
  mkdir -p /etc/aichat
  if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -C "sophoschat-deploy@$(hostname)" -f "$KEY" >/dev/null
  fi
  chmod 600 "$KEY"
  cat > "$CONF" <<EOF
REPO=$REPO
BRANCH=main
EOF
  chmod 600 "$CONF"
  say "把下面这行公钥加到 GitHub：仓库 → Settings → Deploy keys → Add deploy key（不要勾 Allow write access）"
  cat "$KEY.pub"
  echo
  echo "加好之后运行："
  echo "  bash $0 --clone"
  exit 0
fi

[ -f "$CONF" ] || die "还没设置，先运行：update.sh --setup git@github.com:用户/仓库.git"
# shellcheck disable=SC1090
. "$CONF"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/etc/aichat/known_hosts"

if [ "${1:-}" = "--clone" ]; then
  [ -d "$SRC/.git" ] || git clone "$REPO" "$SRC"
  say "克隆完成，运行更新：bash $SRC/deploy/update.sh"
  exit 0
fi

REF="${1:-$BRANCH}"
[ -d "$SRC/.git" ] || die "没有源码目录，先运行 --clone"

exec > >(tee -a "$LOG") 2>&1

# ---------------------------------------------------------------- 拉取
say "拉取 GitHub 最新代码（$REF）"
git -C "$SRC" fetch --tags --prune origin
if git -C "$SRC" rev-parse --verify -q "origin/$REF" >/dev/null; then TARGET="origin/$REF"; else TARGET="$REF"; fi
NEW="$(git -C "$SRC" rev-parse --short "$TARGET")" || die "找不到 $REF"
CUR="$(cat /opt/aichat/.deployed_commit 2>/dev/null || echo none)"
echo "当前：$CUR  →  目标：$NEW"
if [ "$CUR" = "$NEW" ] && [ -z "${FORCE:-}" ]; then
  echo "已经是最新，无需更新（强制重装：FORCE=1 bash $0）"; exit 0
fi
[ "$CUR" != none ] && git -C "$SRC" log --oneline "$CUR..$NEW" 2>/dev/null | head -20 || true

# ---------------------------------------------------------------- 打包
TS="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
ZIP="$WORK/aichat-app.zip"
git -C "$SRC" archive --format=zip --prefix=aichat-app/ -o "$ZIP" "$TARGET"

# ---------------------------------------------------------------- 备份
say "备份数据"
BK="/var/backups/aichat-pre-$NEW-$TS"
mkdir -p /var/backups
cp -a /var/lib/aichat "$BK"
echo "备份到 $BK"
# 只保留最近 5 份升级备份
ls -dt /var/backups/aichat-pre-* 2>/dev/null | tail -n +6 | xargs -r rm -rf

# ---------------------------------------------------------------- 升级
say "升级安装"
# 沿用现有部署参数（自有 nginx :8080 + 现有证书）；可在 /etc/aichat/update.env 里覆盖
: "${DOMAIN:=srv822444.hstgr.cloud}"
: "${PUBLIC_PORT:=8080}"
: "${TLS_CERT:=/etc/letsencrypt/live/$DOMAIN/fullchain.pem}"
: "${TLS_KEY:=/etc/letsencrypt/live/$DOMAIN/privkey.pem}"
OWNER_USER="${OWNER_USER:-ba}" OWNER_PASS=unused-in-upgrade \
USER2="${USER2:-liu}" USER2_PASS=unused-in-upgrade \
DOMAIN="$DOMAIN" PROXY="${PROXY:-own-nginx}" PUBLIC_HOST="$DOMAIN" PUBLIC_PORT="$PUBLIC_PORT" \
TLS_CERT="$TLS_CERT" TLS_KEY="$TLS_KEY" APP_PORT="${APP_PORT:-8765}" SKIP_UFW=1 \
  bash -c "cd '$WORK' && unzip -q aichat-app.zip && cd aichat-app && bash deploy/install.sh '$ZIP'" \
  < /dev/null > "$WORK/install.log" 2>&1 || { tail -40 "$WORK/install.log"; die "安装失败，数据备份在 $BK，日志见上"; }
cp "$WORK/install.log" "/var/log/aichat-install-$TS.log"

# ---------------------------------------------------------------- 检查
say "健康检查"
sleep 3
FAIL=0
for s in aichat aichat-bridge aichat-proxy; do
  st="$(systemctl is-active "$s" 2>/dev/null || true)"
  printf '%-16s %s\n' "$s" "$st"
  [ "$st" = active ] || FAIL=1
done
code="$(curl -ks -o /dev/null -w '%{http_code}' "http://127.0.0.1:${APP_PORT:-8765}/api/health" || true)"
echo "api/health       $code"
[ "$code" = 200 ] || FAIL=1
[ "$FAIL" = 0 ] || die "有服务异常。查看：journalctl -u aichat -n 50；回滚数据：见 $BK"

echo "$NEW" > /opt/aichat/.deployed_commit
say "更新完成 → $NEW"
echo "手机端刷新页面即可（PWA 若没变，彻底关掉再打开一次）"
