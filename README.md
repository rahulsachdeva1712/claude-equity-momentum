# claude-equity-momentum

Single-user trading app for a long-only BSE daily momentum strategy.
Two modules: **paper trading** (always on) and **live trading** (Dhan, switchable). Two tabs in the UI.

See [`docs/FRD.md`](docs/FRD.md) for the complete functional spec. That is the
source of truth; this README is a pointer.

## Status

Scaffold only. No trading logic yet. Do NOT point this at a real account.

## Install (dev)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

**Windows (one-click):** double-click `run.bat`. First run creates the venv, installs deps, and opens your editor on `%USERPROFILE%\Documents\shared\.env` for you to paste Dhan credentials. Second run launches worker + web and opens the UI.

**Linux / macOS (one-click):** `./run.sh`. Same sequence.

**Stop:** `stop.bat` / `./stop.sh` — sends SIGTERM so PID files are cleaned (FRD B.10). If the app is killed forcibly, the next launch detects and cleans the stale PID file automatically.

**Manual run:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# .env lives at ~/Documents/shared/.env so sibling tools running against
# the same Dhan account share one credentials file. Override with the
# EMRB_ENV_FILE env var if you want it elsewhere.
mkdir -p ~/Documents/shared
cp .env.example ~/Documents/shared/.env   # if it doesn't exist yet
chmod 600 ~/Documents/shared/.env
# edit ~/Documents/shared/.env, fill in DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN

# Two terminals
emrb-worker    # scheduler + Dhan writes
emrb-web       # UI at http://127.0.0.1:8766
```

Startup automatically cleans stale PID files from a prior crash
(see FRD B.10).

## Layout

```
app/
  settings.py          env loading, paths
  paths.py             ~/Documents/shared/.env + ~/.claude-equity-momentum/* helpers
  time_utils.py        IST helpers
  pidfile.py           stale-aware PID files
  db.py                SQLite schema
  charges.py           Dhan CNC charge stack
  redaction.py         log redaction filter
  dhan/                HTTP client, models
  strategy/            indicators + signal engine
  paper/               paper engine
  live/                live engine + reconciliation
  worker/              daemon entry, scheduler, jobs
  web/                 FastAPI app, templates
tests/
docs/
```
