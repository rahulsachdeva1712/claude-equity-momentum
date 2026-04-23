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
if [ ! -f "$STATE_DIR/.env" ]; then
    cp .env.example "$STATE_DIR/.env"
    chmod 600 "$STATE_DIR/.env"
    echo
    echo "First-time setup: paste DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN into"
    echo "  $STATE_DIR/.env"
    echo "Then rerun this script."
    ${EDITOR:-vi} "$STATE_DIR/.env"
    exit 0
fi

# Launch worker + web. Log to state dir; store background PIDs for stop.sh.
WORKER_LOG="$STATE_DIR/logs/worker.out"
WEB_LOG="$STATE_DIR/logs/web.out"
mkdir -p "$STATE_DIR/logs"

./.venv/bin/emrb-worker >"$WORKER_LOG" 2>&1 &
WORKER_BG=$!
sleep 2
./.venv/bin/emrb-web >"$WEB_LOG" 2>&1 &
WEB_BG=$!

echo "$WORKER_BG" > "$STATE_DIR/run/launcher-worker.bgpid"
echo "$WEB_BG"    > "$STATE_DIR/run/launcher-web.bgpid"

sleep 2
URL="http://127.0.0.1:18765"
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
