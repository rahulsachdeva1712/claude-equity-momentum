#!/usr/bin/env bash
# One-click launcher for Linux / macOS. Mirrors run.bat.
# See docs/FRD.md B.2 (processes) and B.10 (stale PID cleanup).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip >/dev/null
    echo "Installing project (first run only)..."
    ./.venv/bin/pip install -e ".[dev]"
fi

STATE_DIR="$HOME/.claude-equity-momentum"
mkdir -p "$STATE_DIR"

# Credentials live at $HOME/Documents/shared/.env so the same file can be
# consumed by sibling tools running against the same Dhan account. Override
# with the EMRB_ENV_FILE env var if needed. Runtime state stays in
# $STATE_DIR (db, logs, pid files).
SHARED_DIR="$HOME/Documents/shared"
ENV_PATH="$SHARED_DIR/.env"
mkdir -p "$SHARED_DIR"
if [ ! -f "$ENV_PATH" ]; then
    if [ -f "$ROOT/.env.example" ]; then
        cp "$ROOT/.env.example" "$ENV_PATH"
    else
        cat >"$ENV_PATH" <<'EOF'
# Credentials file. Paste a fresh Dhan access token daily.
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
EOF
    fi
    chmod 600 "$ENV_PATH"
    echo
    echo "First-time setup: paste DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN into"
    echo "  $ENV_PATH"
    echo "Then rerun this script."
    ${EDITOR:-vi} "$ENV_PATH"
    exit 0
fi

# Launch worker + web. Log to state dir; store background PIDs for stop.sh.
WORKER_LOG="$STATE_DIR/logs/worker.out"
WEB_LOG="$STATE_DIR/logs/web.out"
mkdir -p "$STATE_DIR/logs" "$STATE_DIR/run"

./.venv/bin/emrb-worker >"$WORKER_LOG" 2>&1 &
WORKER_BG=$!
sleep 2
./.venv/bin/emrb-web >"$WEB_LOG" 2>&1 &
WEB_BG=$!

echo "$WORKER_BG" > "$STATE_DIR/run/launcher-worker.bgpid"
echo "$WEB_BG"    > "$STATE_DIR/run/launcher-web.bgpid"

sleep 2
URL="http://127.0.0.1:8766"
if command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null; then open "$URL" >/dev/null 2>&1 || true
fi

cat <<EOF
=====================================================================
 Equity Momentum Rebalance is running.
 - UI: $URL
 - Stop: ./stop.sh
 - Logs: $WORKER_LOG
         $WEB_LOG
=====================================================================
EOF
