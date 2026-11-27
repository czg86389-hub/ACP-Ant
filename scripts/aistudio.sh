#!/bin/bash
# ACP-Ant 第二路来源：Google AI Studio，由 AIStudio2API（https://github.com/Mag1cFall/AIStudio2API）提供。
#   aistudio.sh setup     启动 ACP-Ant 专用的 AIStudio2API 容器（只监听本机），生成它的密钥，并让网关使用它
#   aistudio.sh status    查看容器和服务状态
#   aistudio.sh remove    停用：删除容器、网关不再使用 AI Studio（数据目录保留；加 --purge 一并删除）
# 账号在网页控制面板里添加：「＋ 接入 AI Studio」→ 在服务器上的浏览器里登录 Google（画面显示在面板中）。
set -euo pipefail
ROOT=${AGY_ROOT:-/opt/acp-ant}
NAME=${ACPANT_STUDIO_CONTAINER:-acp-aistudio}
PORT=${ACPANT_STUDIO_PORT:-2051}
IMAGE=${ACPANT_STUDIO_IMAGE:-ghcr.io/mag1cfall/aistudio2api@sha256:ac20978473d1149acfd240bae844dac13001d1363cd6c73178d67af2fb5b55ff}
DATA=$ROOT/aistudio/data
CFG=$ROOT/studio.json
ENVFILE=$ROOT/gateway.env
die() { echo "错误：$*" >&2; exit 1; }
say() { echo "==> $*"; }
DOCKER=docker
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then DOCKER="sudo docker"
  else die "需要 Docker（并且当前用户能运行 docker，或有免密 sudo）"; fi
fi
[ -d "$ROOT" ] || die "$ROOT 不存在：先用 ./install.sh 安装 ACP-Ant（或设置 AGY_ROOT）"

# The gateway's systemd unit: ACPANT_SERVICE, else acp-ant, else the unit whose command runs from $ROOT
# (installs that were set up by hand under another name).
service_name() {
  local scope=$1 u
  if [ -n "${ACPANT_SERVICE:-}" ]; then echo "$ACPANT_SERVICE"; return; fi
  if systemctl $scope cat acp-ant >/dev/null 2>&1; then echo acp-ant; return; fi
  for u in $(systemctl $scope list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}'); do
    if systemctl $scope cat "$u" 2>/dev/null | grep -E '^ExecStart=' | grep -qF -- "$ROOT"; then echo "${u%.service}"; return; fi
  done
}

restart_gateway() {
  local mode svc
  mode=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("mode",""))' "$ROOT/install-info.json" 2>/dev/null || true)
  if [ "$mode" = "system" ]; then
    svc=$(service_name "")
    [ -n "$svc" ] || { echo "没有找到网关的 systemd 服务，请手动重启网关（或设置 ACPANT_SERVICE）"; return; }
    if [ "$(id -u)" = 0 ]; then systemctl restart "$svc"; else sudo systemctl restart "$svc"; fi
  elif [ "$mode" = "user" ]; then
    svc=$(service_name --user)
    [ -n "$svc" ] || { echo "没有找到网关的 systemd 服务，请手动重启网关（或设置 ACPANT_SERVICE）"; return; }
    systemctl --user restart "$svc"
  else echo "请手动重启网关，使配置生效"; return; fi
  say "网关（$svc）已重启"
}

case "${1:-}" in
  setup)
    mkdir -p "$DATA"; chmod 700 "$ROOT/aistudio" "$DATA"
    if [ ! -f "$DATA/.env" ]; then
      ( umask 077
        printf 'LISTEN_ADDR=0.0.0.0:2048\nPREVIEW_LISTEN_ADDR=127.0.0.1:2050\nPROXY_API_KEY=sk-acp-studio-%s\nADMIN_AUTH_ENABLED=true\nADMIN_PASSWORD=%s\nAUTO_START=true\nREQUEST_BODY_LOG=false\nLOG_FILE=false\nWAA_BACKEND=spidermonkey\nROUTING_STRATEGY=round-robin\nWARM_WORKER_LIMIT=30\nMAX_ACTIVE_WORKERS=30\nWARM_STARTUP_CONCURRENCY=5\nINIT_TIMEOUT=2m\nREQUEST_TIMEOUT=5m\nNONSTREAM_KEEPALIVE=false\n' \
          "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > "$DATA/.env" )
      say "已生成 AIStudio2API 的密钥（只保存在 $DATA/.env，权限 600）"
    fi
    if ! $DOCKER ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
      say "启动容器 $NAME（127.0.0.1:$PORT，第一次会下载镜像）"
      $DOCKER run -d --name "$NAME" --restart unless-stopped --init -p "127.0.0.1:$PORT:2048" -v "$DATA:/data" \
        "$IMAGE" aistudio2api -open-ui=false >/dev/null
    else
      $DOCKER start "$NAME" >/dev/null || true
    fi
    for _ in $(seq 1 60); do
      code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/auth/session" || true)
      case "$code" in 200|401) break ;; esac; sleep 2
    done
    case "${code:-}" in 200|401) ;; *) die "AIStudio2API 没有启动起来，查看：$DOCKER logs $NAME" ;; esac
    ( umask 077
      python3 - "$DATA/.env" "$CFG" "$PORT" <<'PY'
import json, os, sys
env = dict(l.split('=', 1) for l in open(sys.argv[1]).read().splitlines() if '=' in l)
tmp = sys.argv[2] + '.tmp'
with open(tmp, 'w') as f:
    json.dump({'base_url': 'http://127.0.0.1:%s' % sys.argv[3], 'api_key': env['PROXY_API_KEY'],
               'admin_password': env['ADMIN_PASSWORD']}, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, sys.argv[2])
PY
    )
    grep -q '^ACPANT_STUDIO_CONFIG=' "$ENVFILE" 2>/dev/null || echo "ACPANT_STUDIO_CONFIG=$CFG" >> "$ENVFILE"
    restart_gateway
    say "完成。打开网页控制面板 →「＋ 接入 AI Studio」，填 Google 邮箱，在面板里的服务器浏览器中登录。"
    echo "    第一次登录前 AIStudio2API 会下载约 1.2 GB 的浏览器（Camoufox），需要几分钟。"
    ;;
  status)
    $DOCKER ps -a --filter "name=^${NAME}$" --format '{{.Names}}  {{.Status}}  {{.Ports}}'
    grep -q '^ACPANT_STUDIO_CONFIG=' "$ENVFILE" 2>/dev/null && echo "网关已配置使用 AI Studio（$CFG）" || echo "网关没有配置 AI Studio"
    ;;
  remove)
    $DOCKER rm -f "$NAME" >/dev/null 2>&1 || true
    if [ -f "$ENVFILE" ]; then sed -i '/^ACPANT_STUDIO_CONFIG=/d' "$ENVFILE"; fi
    rm -f "$CFG"
    [ "${2:-}" = "--purge" ] && { $DOCKER run --rm -v "$ROOT/aistudio:/x" alpine rm -rf /x/data >/dev/null 2>&1 || rm -rf "$DATA"; say "数据目录已删除"; }
    restart_gateway
    say "已停用 AI Studio 第二路"
    ;;
  *) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
