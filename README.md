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

```bash
# First time: create state dir and paste creds
mkdir -p ~/.claude-equity-momentum
cp .env.example ~/.claude-equity-momentum/.env
chmod 600 ~/.claude-equity-momentum/.env
# edit the file, fill in DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN

# Two terminals
emrb-worker    # scheduler + Dhan writes
emrb-web       # UI at http://127.0.0.1:8765
```

Startup automatically cleans stale PID files from a prior crash
(see FRD B.10).

## Layout

```
app/
  settings.py          env loading, paths
  paths.py             ~/.claude-equity-momentum/* helpers
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
