#!/bin/bash
# Container start: first run creates the API key and installs the official agy CLI into the data volume,
# then the gateway runs as the unprivileged user "acpant" (uid 1000).
set -euo pipefail
D=${AGY_ROOT:-/data}
AGY=${AGY_BIN:-$D/bin/agy}
mkdir -p "$D/bin" "$D/accounts"

if [ ! -f "$D/gateway.env" ]; then
  echo "==> first start: writing $D/gateway.env with a new API key"
  ( umask 077
    { echo "AGY_GATEWAY_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
      echo "# requests at once per account (the panel can change it), request timeout (s), keep local conversation files (h)"
      echo "AGY_PER_ACCOUNT=3"
      echo "AGY_REQUEST_TIMEOUT=290"
      echo "AGY_KEEP_HOURS=6"
    } > "$D/gateway.env" )
fi

if [ ! -x "$AGY" ]; then
  # in the volume, not the image: agy updates itself, and the update must survive a container rebuild
  echo "==> installing the official Antigravity CLI (agy) into $D/bin"
  tmp=$(mktemp)
  curl -fsSL https://antigravity.google/cli/install.sh -o "$tmp"
  bash "$tmp" --dir "$D/bin"
  rm -f "$tmp"
  [ -x "$AGY" ] || { echo "agy install failed" >&2; exit 1; }
fi

# Unlock gates (1M context, 192KB clamp removal) in official agy CLI binary
if [ -f "/app/scripts/agy-patch.py" ] && [ -x "$AGY" ]; then
  echo "==> unlocking context window & token clamp gates on $AGY..."
  python3 /app/scripts/agy-patch.py --binary "$AGY" --in-place 2>/dev/null || true
fi

# gateway.env holds defaults; values given to the container (docker-compose environment:) win
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  key=${line%%=*}
  [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
  [ -n "${!key+x}" ] || export "$key=${line#*=}"
done < "$D/gateway.env"

RUN=(uvicorn acp_ant:app --app-dir /app --host "${AGY_HOST:-0.0.0.0}" --port "${AGY_PORT:-8095}" --log-level warning)
if [ "$(id -u)" = 0 ]; then
  [ "$(stat -c %u "$D")" = 1000 ] && [ "$(stat -c %u "$D/accounts")" = 1000 ] || chown -R 1000:1000 "$D"
  chmod 700 "$D/accounts"
  exec setpriv --reuid=1000 --regid=1000 --init-groups env HOME=/home/acpant "${RUN[@]}"
fi
chmod 700 "$D/accounts" 2>/dev/null || true
exec "${RUN[@]}"
