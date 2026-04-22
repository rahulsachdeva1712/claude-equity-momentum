#!/usr/bin/env bash
# Graceful stop for Linux / macOS. Sends SIGTERM to worker + web which
# route through pidfile.release() per FRD B.10.
set -euo pipefail

STATE_DIR="$HOME/.claude-equity-momentum"
for name in worker web; do
    pid_file="$STATE_DIR/run/${name}.pid"
    if [ -f "$pid_file" ]; then
        pid=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('pid',''))" "$pid_file" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "stopping $name (pid $pid)..."
            kill -TERM "$pid" || true
        fi
    fi
done

echo "done. Remaining stale PID files (if any) will be cleaned on next run.sh."
