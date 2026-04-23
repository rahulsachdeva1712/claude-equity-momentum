# Equity Momentum Rebalance — Functional Requirements Document

Living document. Source of truth going forward.
Last updated: 2026-04-22.

This FRD has two parts:
- **Part A — Strategy Specification.** Preserved verbatim from `Equity_Momentum_App_FRD.docx` (dated 2026-04-22). Describes the trading rules only.
- **Part B — Application Functional Requirements.** Covers everything the strategy doc intentionally excluded: paper module, live module, Dhan integration, reconciliation, scheduling, UI, processes, persistence, failure handling.

---

# Part A — Strategy Specification

Equity Momentum Rebalance Strategy. Code-derived strategy document generated from the current repository on 2026-04-22. This document is limited to the strategy itself: universe, inputs, filters, entry rules, exit rules, position sizing, and rebalance criteria. It intentionally avoids broader application, deployment, and backtest-operating details. Timing references are included only where they affect paper and live trading behavior.

## A.1 Strategy Overview
- Strategy type: long-only daily momentum strategy.
- Market: BSE equities only.
- Configured universe: `all_bse_equities`.
- Portfolio style: concentrated portfolio with a maximum of 5 holdings.
- Selection approach: eligible stocks are ranked by 6-month relative return and weighted by 12-month relative return.
- Current execution assumption for paper and live workflow: signal at 09:10 IST, intended trade time at 09:30 IST on the next session.

## A.2 Universe Definition
- Universe name in code: `all_bse_equities`.
- Exchange scope: BSE only.
- A symbol must have daily OHLCV data available to participate.
- A symbol must have the configured ranking metric and weighting metric available for the signal date to remain eligible.
- A symbol must have a usable `market_cap_cr` value to pass the active market-cap filter.

## A.3 Strategy Inputs And Derived Metrics
- Raw data inputs: daily open, high, low, close, volume, symbol, `security_id`, `exchange_segment`, `market_cap_cr`.
- EMA fast: exponential moving average of close over `ema_fast_period`.
- EMA slow: exponential moving average of close over `ema_slow_period`.
- RSI: relative strength index over `rsi_period`.
- MFI: money flow index over `mfi_period`.
- CCI: commodity channel index over `cci_period`.
- ATR percent: average true range over `atr_period` divided by close.
- Absolute return metrics: `return_63d`, `return_126d`, `return_252d`.
- Relative return metrics: `relative_return_63d`, `relative_return_126d`, `relative_return_252d`.
- Relative return definition: symbol return minus same-day average universe return for the same lookback window.

## A.4 Current Input Parameters
| Parameter | Current Value |
|---|---|
| Universe | `all_bse_equities` |
| Initial market scope | BSE equities only |
| RSI period | 14 |
| MFI period | 14 |
| CCI period | 14 |
| ATR period | 20 |
| EMA fast period | 21 |
| EMA slow period | 50 |
| Use RSI filter | True |
| Use MFI filter | False |
| Use CCI filter | False |
| Use ATR filter | True |
| RSI operator and threshold | `>= 75.0` |
| MFI operator and threshold | `>= 70.0`, currently inactive |
| CCI operator and threshold | `>= 110.0`, currently inactive |
| ATR operator and threshold | `<= 0.04` |
| Price vs EMA operator | `close > EMA(21)` |
| EMA stack operator | `EMA(21) > EMA(50)` |
| Sort metric | `relative_return_126d`, descending |
| Weight metric | `relative_return_252d` |
| Maximum positions | 5 |
| Minimum positions | 1 |
| Full rebalance | True |
| Market-cap threshold | 100.0 crore |
| Breadth threshold | 0.0, therefore breadth gate disabled |
| Signal generation time | 09:10 IST |
| Execution time | 09:30 IST |
| Execution model | `next_day_open` |
| Explicit transaction cost | 10 basis points |

## A.5 Active Filters
- Market-cap filter: `market_cap_cr >= 100.0`.
- Trend filter 1: `close > EMA(21)`.
- Trend filter 2: `EMA(21) > EMA(50)`.
- Momentum filter: `RSI(14) >= 75`.
- Volatility filter: `ATR(20) percent <= 4 percent`.
- Breadth gate: configured in code but inactive because `breadth_threshold = 0.0`.
- MFI gate: configured in code but inactive because `use_mfi_filter = False`.
- CCI gate: configured in code but inactive because `use_cci_filter = False`.

## A.6 Entry Criteria
A stock becomes a long candidate for a signal date only if all of the following are true on that date:
- The stock belongs to the `all_bse_equities` universe and has usable daily data.
- `market_cap_cr >= 100.0`.
- `close > EMA(21)`.
- `EMA(21) > EMA(50)`.
- `RSI(14) >= 75`.
- `ATR(20) percent <= 4 percent`.
- The configured sort metric `relative_return_126d` is present.
- The configured weight metric `relative_return_252d` is present.

After eligibility is established: all eligible names are ranked by `relative_return_126d` in descending order, and only the top 5 names are selected. If fewer than 1 selected name remains, no portfolio is formed for that signal date.

## A.7 Position Sizing Rules
- Selected names are weighted using `relative_return_252d`.
- Any negative weight metric is clipped to zero before sizing.
- If the clipped weight metric sum is positive, target weights are proportional to the clipped values.
- If the clipped weight metric sum is zero, all selected names receive equal weight.
- Target values are converted into integer shares by `floor(target_value / execution_price)`.
- Trading cost is deducted at 10 bps of traded value, so final shares are cash-aware.

## A.8 Exit Criteria
The code does not use a separate stop-loss or profit-target exit model. Exits happen through rebalance logic. A position is reduced or closed when any of the following applies on the next rebalance event:
- The symbol is no longer selected in the top ranked target set for the latest signal date, in which case target quantity becomes zero and the position is exited.
- The symbol remains selected but its newly computed target quantity is lower than the current quantity, in which case the position is trimmed down to target.
- The rebalance engine cannot justify retaining the prior quantity after the new signal weights and cash-aware target sizing are applied.

A stock can effectively lose its place in the portfolio because it fails the active filters, loses ranking position, or receives a smaller target weight after re-ranking. The code treats that outcome through target-quantity reconciliation rather than through a standalone exit rule module.

## A.9 Rebalancing Criteria
- Rebalance frequency in the current configuration is every 1 trading session.
- The strategy is configured for `full_rebalance = True`.
- At each rebalance event, the portfolio compares current holdings against the new target quantities from the latest signal set.
- Deselected symbols are sold to zero if tradable on the execution date.
- Continuing symbols are either held, trimmed, or topped up until target quantities are reached.
- Newly selected symbols are bought if there is tradable data and sufficient cash after cost.
- If a symbol has no tradable bar on the execution day, the engine skips that entry for that session.

## A.10 Paper And Live Trading Timing Relevance
- The strategy prepares the daily signal using completed-day data at 09:10 IST.
- The strategy is intended to be applied at 09:30 IST for paper and live trading workflows.
- Paper and live trading therefore consume the same morning target set: what to buy, what to trim, what to hold, and what to exit.

## A.11 Strategy Constraints And Known Limitations
- The strategy is long-only.
- The strategy is daily only and does not include intraday entry logic.
- There is no hard stop-loss, take-profit, trailing stop, or hedge overlay in the strategy code.
- The market-cap filter uses current-state market-cap data rather than point-in-time historical market-cap snapshots.
- Integer-share sizing and trading-cost deductions can cause actual realized weights to differ from ideal target weights.

## A.12 Source Basis
- `src/config.py`
- `src/indicators.py`
- `src/strategy.py`
- `src/backtest.py`
- `artifacts/equity_backtest_snapshot.json`

Note: these source files do not yet exist in the repository. Part A is treated as the authoritative strategy specification; Part B code will implement it.

---

# Part B — Application Functional Requirements

## B.1 Scope and Modules

The application is a single-user desktop-style web app that runs on the user's machine during Indian market hours. It wraps the Part A strategy in two modules:

1. **Paper Trading** — continuously maintained virtual book that mirrors what the live module would have done, using the same signals and execution timing. Always on.
2. **Live Trading** — places actual CNC delivery orders on Dhan at 09:30 IST per the daily signal. Can be switched off. When off, no live orders are placed; existing live positions are held and tracked only.

Both modules consume the identical 09:10 signal set produced by the strategy engine. There is no backtest UI in the application. Historical data access remains only to compute the indicators and relative-return metrics the strategy needs.

UI exposes exactly two tabs: **Paper Trading** and **Live Trading**. A persistent top bar provides global status and a settings modal.

## B.2 Process Topology

Two OS processes, one SQLite database, one filesystem state directory `~/.claude-equity-momentum/`.

| Process | Role | PID file |
|---|---|---|
| `worker` | Long-running daemon. Owns all Dhan writes. Runs APScheduler, live reconciliation loop, token expiry watcher, paper book maintenance. Single writer to SQLite. | `run/worker.pid` |
| `web` | FastAPI + Jinja + HTMX + Plotly. Renders UI. Reads SQLite. Writes only to the `settings` table (kill switch, worker control) and to `~/.claude-equity-momentum/.env` on token update. | `run/web.pid` |

Scheduler jobs, live reconciliation, and the token watcher are **threads inside the worker process**, not separate OS processes. They share `worker.pid`. Rationale: fewer things to supervise, atomic shutdown, no IPC complexity.

Worker and web communicate via SQLite (`settings` table flags polled on a 2-second cadence from worker) and via a small `run/commands/` inbox for one-shot signals (e.g., "run rebalance now"). No network IPC.

## B.3 Persistence

- SQLite file: `~/.claude-equity-momentum/state.db`.
- WAL mode enabled to allow concurrent reads from web while worker writes.
- Single writer invariant: only the worker process executes `INSERT` / `UPDATE` / `DELETE` outside the `settings` table.
- Required tables (exact schema finalized during build, listed here for scope):
  - `signals` — one row per signal date per symbol with selection flag, rank, target weight, target qty.
  - `paper_orders` — generated paper orders (buy/trim/exit).
  - `paper_fills` — paper fills with 09:30 close price, qty, charges breakdown.
  - `paper_book` — current paper holdings.
  - `paper_pnl_daily` — per-day realized, unrealized, MTM.
  - `live_orders` — orders we sent to Dhan, each with a `correlation_id` tag, status, rejects, alerts.
  - `live_fills` — fills we pulled from Dhan for our tagged orders only.
  - `live_positions_snapshot` — periodic snapshot of Dhan positions filtered by tag.
  - `live_pnl_daily` — per-day realized and unrealized from tagged positions.
  - `charges` — per-fill charges breakdown, stored for both modules.
  - `alerts` — user-facing notifications (rejects, token-expiry, market-closed, stale-proc cleanup, errors).
  - `settings` — single-row key/value for kill switch, worker control, UI preferences.
  - `audit_log` — append-only log of every Dhan write (order placement, modify, cancel) with request/response bodies redacted of secrets.

Historical OHLCV is **not** persisted. Each 09:10 run fetches the required lookback window from Dhan historical API and computes indicators in memory. This avoids stale-cache risk.

## B.4 Authentication and Credentials

- Access token is stored in `~/.claude-equity-momentum/.env` under `DHAN_ACCESS_TOKEN=...`.
- Other required secrets: `DHAN_CLIENT_ID`.
- **TOTP secret and PIN are NOT used.** Dhan's official v2 API uses a manually issued access token. Automated TOTP login is unofficial and out of scope.
- User action: generate a fresh access token from the Dhan web portal each morning and paste it into `.env`. The worker watches the file (mtime polled every 10s) and hot-reloads on change.
- On startup the worker validates the token by calling a cheap Dhan read endpoint (e.g., fund limits). Invalid or expired token → worker stays alive, live trading automatically disabled, alert raised, UI shows "Token invalid/expired." Paper trading is unaffected.
- Token expiry is parsed from the JWT `exp` claim. UI top bar shows "Token valid, expires in Xh Ym." 60 minutes before expiry an alert is raised. 10 minutes before expiry a second alert is raised.
- Secrets never logged. `.env` is git-ignored. Redaction layer scrubs tokens from every log line and every `audit_log` row.

## B.5 Market Calendar and Scheduler

- No pre-baked holiday calendar. Source of truth for "is the market open today" is Dhan's market-status API, queried at each scheduled trigger.
- APScheduler runs inside the worker with IST timezone.
- Daily jobs:
  - **09:10 IST — Signal job.** Query Dhan market-status. If closed, log and exit. Otherwise fetch required OHLCV lookback, compute indicators and relative returns, rank, select up to 5, compute target weights. Write `signals` rows. Emit paper orders to `paper_orders` (buy/trim/exit) derived from diff between current `paper_book` and new targets using last known close for sizing. This is the paper "intent" — fills land at the 09:30 job.
  - **09:30 IST — Execution job.** Query Dhan market-status. If closed, log and exit. For each paper order, fetch the 09:30 candle close for the symbol from Dhan; record `paper_fills` with full Dhan charge stack; update `paper_book`. If live trading is enabled (kill switch off), for each live order, place a CNC market order on Dhan tagged with `correlation_id`; subscribe to order status; on fill, update `live_fills` and then **adjust the matching `paper_fills` qty to match the actual live fill qty** (the parity rule). On reject, skip the symbol, raise an alert, do not retry.
- Live reconciliation loop: every 15 seconds during market hours (09:15–15:30 IST), the worker pulls Dhan positions and order book, filters to our `correlation_id` tags, updates `live_positions_snapshot` and `live_pnl_daily`. Outside market hours the loop is idle.
- Token expiry watcher: every 60 seconds, re-parse `exp`, raise alerts at the 60-min and 10-min thresholds, disable live trading if expired.
- Catch-up behavior: if the worker is not running at 09:10 or 09:30, those jobs do not run retroactively. On next start, UI shows "Missed today's signal/execution" alert. No auto-catch-up, because mid-day fills would diverge from the specified 09:30-close price.

## B.6 Paper Trading Module

**Purpose.** Always-on virtual book that applies the strategy identically to live, with two goals: (a) operate when live is switched off, (b) serve as the "shadow" book whose numbers match live within rounding when live is on.

**Inputs.** Daily signal set from the 09:10 job; Dhan historical / 1-minute candle API for the 09:30 candle close.

**Fill rule.** Every paper order fills at the **close of the 09:30 minute candle** for the corresponding symbol. If Dhan returns no 09:30 candle for a symbol (halt, no trade), that symbol is skipped for that session and an alert is raised — matching the strategy's "no tradable bar" rule in A.9.

**Charges.** Paper fills apply the full Dhan charge stack for CNC delivery:
- Brokerage (Dhan's publicly posted CNC rate — currently zero for delivery, but computed via a pluggable function so a future change does not require a code change).
- STT / CTT.
- Exchange transaction charges (BSE rate).
- SEBI turnover fee.
- Stamp duty.
- GST on brokerage + transaction + SEBI.

Breakdown persisted per `paper_fills` row so UI can show "Non broker charge" identically to live. The charges function is the **same code path** used by the live module when live fills arrive — both read from one shared `charges.py`.

**Parity with live.** When live trading is enabled and a live fill lands at quantity `q_live` (possibly a partial fill less than ordered), the paper fill for the same symbol for that session is **adjusted to `q_live`** before MTM is computed. If live is disabled, paper fills at the intended target quantity. This is the "paper follows live quantity" rule.

**Corporate actions.** No adjustments applied to the paper book. If a position exists on record date, the paper quantity stays as-is through the event. Live side is already reconciled from the Dhan positions book so it reflects whatever Dhan applies. This intentionally lets the two drift when a corporate action happens — documented limitation, not a bug.

**MTM.** Paper unrealized PnL uses Dhan LTP for each paper holding, pulled at the same cadence as live reconciliation (15 seconds during market hours). Outside market hours, MTM uses last available close.

**Outputs.** `paper_orders`, `paper_fills`, `paper_book`, `paper_pnl_daily`, `charges`, `alerts`.

## B.7 Live Trading Module

**Enablement.** A single boolean in `settings.live_enabled`. Default is **off** on fresh install. Toggled only from the UI settings modal. The worker reads this flag at every 09:30 job and during recon. Turning it off mid-day does **not** square off; existing live positions are held and tracked (per decision #15).

**Order placement.** At the 09:30 job, for each target diff (buy, trim, exit), the worker places a Dhan CNC market order with:
- `transactionType` = BUY or SELL
- `productType` = CNC
- `orderType` = MARKET
- `exchangeSegment` = BSE_EQ
- `correlationId` = `emrb:<session_date>:<symbol>:<action>:<short_uuid>` (the tag)

The `correlationId` prefix `emrb:` is the app's ownership marker. Reconciliation and MTM use only orders/positions traceable to this tag.

**Order statuses tracked.** PENDING, TRANSIT, OPEN, TRADED, REJECTED, CANCELLED. Worker polls order status for each outgoing order until terminal (TRADED/REJECTED/CANCELLED) or until 15:30 IST.

**Rejects.** If Dhan rejects an order, the worker logs the reject reason, raises an alert with symbol + reason, and **does not retry**. The matching paper fill also does not happen for that symbol that session (parity). The symbol is effectively absent from the portfolio for that session; the next 09:10 signal decides the next action.

**Partial fills.** If `filledQty < orderedQty` at terminal state, `live_fills` record uses `filledQty`. The matching `paper_fills` is adjusted to `filledQty`. No retry for the residual.

**Kill switch.** Manual only. A single button in the settings modal flips `settings.live_enabled = false`. Effective immediately for any new jobs. An in-flight order placement loop checks the flag between orders and halts placement if flipped during the 09:30 job.

**Trades placed outside the app.** Any Dhan order or position without our `emrb:` `correlationId` is filtered out from every app screen and every computation. The app's live book is not the Dhan book — it is the subset of the Dhan book tagged by us.

**Outputs.** `live_orders`, `live_fills`, `live_positions_snapshot`, `live_pnl_daily`, `charges`, `alerts`, `audit_log`.

## B.8 Reconciliation

**Source of truth for live positions.** Dhan positions book (`/v2/positions`), pulled every 15 seconds during market hours, filtered to rows whose originating order carries our `emrb:` tag. The filter is done by joining to `live_orders.correlation_id`, not by any field on the positions response itself (positions API does not echo `correlationId`; we track it per-order and map).

**Source of truth for live PnL.** Computed by the worker as `sum(live_fills.signed_qty * (ltp - fill_price)) - sum(live_fills.charges)`. This is then cross-checked against `dhan.positions.unrealizedPnL` for tagged rows at the end of each recon cycle. If the two diverge beyond a configurable tolerance (default: 0.5 percent of notional), an alert is raised — this catches missed fills, corporate actions, or tag-mapping bugs.

**Source of truth for live holdings displayed in UI.** Dhan positions book filtered by tag, not the app's internal `live_book` projection. This is per decision #6.

**Daily cutover.** End-of-day (15:35 IST) the worker snapshots `live_positions_snapshot` and freezes `live_pnl_daily` for the date. Overnight holdings carry forward.

**Missing-tag recovery.** If a fill arrives for a `correlation_id` we never sent (should be impossible), the worker logs a critical error, raises a high-severity alert, and excludes the fill from accounting. Manual investigation required.

## B.9 UI

**Framework.** FastAPI + Jinja2 templates + HTMX for partial updates + Plotly (via CDN) for the two charts. Bootstrap for layout. No build step. No SPA.

**Tabs (exactly two).**
1. **Paper Trading** — mirrors the mockup: Today's summary cards, Signals For Today, Today Status, Daily Paper P&L chart, Cumulative Paper P&L chart, Performance Summary, Current Paper Book, Trade log.
2. **Live Trading** — same visual structure as Paper, but numbers come from the reconciled live dataset. When `live_enabled = false`, the tab shows all historical tagged live activity (if any) plus a prominent banner: "Live trading is OFF. Showing held positions only."

**Top bar (persistent, above tabs).**
- App title on the left.
- Status pills on the right:
  - Worker status: `running` / `stopped` / `stale-cleaned`.
  - Dhan token: `valid · expires in Xh Ym` / `expiring soon` / `expired`.
  - Market status: `open` / `closed` / `holiday` (from Dhan market-status).
  - Live switch: `ON` / `OFF` (color-coded; OFF by default).
- Settings icon opens a modal with:
  - Access token status and "Open .env path" helper.
  - Live-enabled toggle (kill switch).
  - Worker start / stop buttons.
  - Alerts inbox (unread count badge on the icon).

**Auto-refresh.** Each tab auto-refreshes every 30 seconds when visible (via HTMX polling). Top-bar status polls every 5 seconds.

**No backtest UI.** Per decision #21.

**No trade entry UI.** The user cannot place, modify, or cancel orders manually. All orders originate from the 09:30 job only.

## B.10 Process Management and Stale Process Handling

**State directory:** `~/.claude-equity-momentum/`
```
~/.claude-equity-momentum/
  .env                        (secrets, chmod 600, git-ignored)
  state.db                    (SQLite)
  logs/
    worker.log
    web.log
  run/
    worker.lock               (sentinel; OS exclusive lock holder)
    worker.pid                (JSON metadata; freely readable)
    web.lock
    web.pid
    commands/                 (one-shot signals, e.g., `run_rebalance.now`)
  artifacts/
    last_signal_<date>.json   (debug snapshot of each 09:10 job)
```

**PID file format.** JSON: `{"pid": 12345, "start_time_epoch": 1712..., "cmd": "worker"}`.
The pid file carries metadata only and is written atomically (tempfile +
rename). The OS exclusive lock is held on the separate `<name>.lock` sentinel
so other processes can read the pid file at any time without colliding with
the lock holder. (Required because Windows uses mandatory file locking;
locking the pid file directly caused PermissionError on read.)

**Startup sequence (both processes).**
1. Check if PID file exists.
2. If it does not exist, proceed to step 6.
3. If it exists, read the PID. Check `/proc/<pid>` (Linux) or `os.kill(pid, 0)` (cross-platform) to see if the process is alive.
4. If alive AND the process command matches our expected command (verified via `/proc/<pid>/comm` or `psutil.Process.name()`), refuse to start, log `already-running`, exit non-zero.
5. If not alive OR command does not match, treat as stale. Log `stale-pid-cleaned` with the old PID, delete the PID file, raise a deferred alert (surfaced in UI on first page load), proceed to step 6.
6. Acquire an OS file lock on the PID file path (fcntl/msvcrt) to prevent a race with a simultaneously-starting second instance.
7. Write our PID / start_time / cmd.
8. Register SIGTERM, SIGINT handlers plus an `atexit` handler. All three paths call the same `shutdown()` routine.

**Shutdown sequence (`shutdown()`).**
1. Set a module-level `SHUTTING_DOWN` flag.
2. Stop accepting new jobs. Wait up to 10 seconds for the current job (if a 09:30 job is mid-flight, we prefer to let it finish).
3. Close Dhan HTTP session and any websocket.
4. Flush SQLite and close the connection.
5. Release the PID file lock and delete the PID file.
6. Exit.

**Crash recovery.** If the worker is killed with `SIGKILL`, step 5 of shutdown does not run. The next startup sees a stale PID and cleans it. That is the only recovery needed; no in-memory state needs replay because all state is in SQLite and all Dhan-side state is reconciled at next recon tick.

**Self-test on startup.** After PID acquisition the worker runs a 30-second self-check: (a) SQLite write+read, (b) Dhan token validation, (c) Dhan market-status call, (d) Dhan historical data sample call. Failures are logged and alerts raised; the worker stays up so the user can see the problem in UI.

**Watchdog.** Web process checks worker liveness every 5 seconds by reading `worker.pid` and `os.kill(pid, 0)`. If the worker dies unexpectedly, the top bar shows `worker: stopped` within 5 seconds. Web does not auto-restart worker; that is a user action.

## B.11 Alerts

All alerts are rows in the `alerts` table. Severity levels: `info`, `warn`, `error`, `critical`. UI shows unread count in the settings icon; modal lists them with acknowledge button.

Mandatory alert sources:
- Token expiring (60 min, 10 min thresholds) and expired.
- Market closed on a scheduled trigger (info).
- Order rejected (error, includes symbol + Dhan reason).
- Partial fill closed at terminal state (info).
- Paper / live divergence beyond tolerance (warn).
- Internal PnL vs Dhan PnL divergence beyond 0.5% notional (warn).
- Fill with unknown `correlation_id` (critical).
- Stale PID cleaned on startup (warn).
- Self-test failure (error).
- Worker unhandled exception (critical).

## B.12 Logging and Audit

- `logs/worker.log`, `logs/web.log` — structured JSON logs, rotated daily, 14 days retained.
- `audit_log` table — append-only, one row per Dhan write call (place, modify, cancel). Stores request params, response body, HTTP status, correlation id, session date. Access token and client id fields are redacted to `***REDACTED***` before insertion.
- All log statements pass through a redaction filter that scrubs JWT tokens, PINs, and TOTP secrets.

## B.13 Non-Functional Requirements

- **Latency budget for the 09:30 job:** place every live order within 60 seconds of 09:30:00 IST, assuming normal Dhan API latency and up to 10 symbols to trade. The job sends orders concurrently (bounded worker pool) with a global timeout.
- **Reliability:** the two-process topology plus SQLite WAL means a UI crash never affects trading, and a worker crash fails fast and surfaces in UI within 5 seconds. No silent failures.
- **Security:** `.env` file permissions forced to 0600 on startup. Secrets never logged. No telemetry leaves the machine.
- **Portability:** Linux and Windows support. Time zone is pinned to Asia/Kolkata inside the scheduler regardless of system locale.
- **Idempotency:** the 09:10 job is idempotent — re-running it for the same session date overwrites `signals` and regenerates `paper_orders` for unfilled items. The 09:30 job is **not** idempotent once live orders are sent; a guard prevents re-run by checking a `sessions.execution_completed` flag.
- **Test coverage target:** golden tests for strategy signal outputs against a fixed dataset; unit tests for charges computation, recon filter, PID file lifecycle; integration test for paper parity with a mocked Dhan client. No tests hit real Dhan.

## B.14 Out of Scope

- Backtest UI and backtest persistence (per decision #21). A CLI-only backtest harness may exist for strategy validation, but is not part of the app surface.
- Multi-user support / auth on the web UI. Single-user local app.
- Automated TOTP-based Dhan login.
- Corporate action auto-adjustment in the paper book.
- Any intraday entry or exit logic beyond the 09:30 rebalance.
- Square-off of live positions on kill-switch off.
- Retry of rejected or partially-filled orders.
- Notification channels outside the app (email/SMS/Telegram). Alerts stay in-app.

## B.15 Open Items and Assumptions

- **Brokerage schedule.** Assumes Dhan CNC brokerage is zero as of the FRD date. The `charges.py` function is pluggable; if Dhan changes this, update the function and document in the change log below.
- **BSE exchange codes in Dhan.** Assumed `exchange_segment = BSE_EQ` for placing and pulling orders/positions. To be verified in the client implementation.
- **1-minute candle availability for 09:30.** Assumes Dhan historical API can return the 09:30 minute candle close for BSE equities within a few minutes after close. If not, fallback is to the 09:30 LTP snapshot polled at `09:30:59`.
- **Session-date definition.** For a signal generated on date `D` at 09:10 using `D-1` close data, the session date is `D` across all tables. All timestamps stored in UTC; displayed in IST.
- **Clock sync.** Assumes host clock is within 2 seconds of true IST. No NTP enforcement in-app.

## B.16 Change Log

| Date | Change | Driven by |
|---|---|---|
| 2026-04-22 | Initial Part B added covering paper, live, Dhan integration, scheduler, processes, UI, data model. | Requirements discussion. |
| 2026-04-22 | Added one-click launchers `run.bat` / `run.sh` and stoppers `stop.bat` / `stop.sh`. First run creates venv, installs deps, seeds `.env`; subsequent runs start worker + web and open the UI. Stoppers send SIGTERM so PID files are cleaned per B.10; forced kills are recovered on next start by the stale-PID cleanup. | User request. |
| 2026-04-23 | Split PID file into a `<name>.lock` sentinel (holds OS exclusive lock) and a `<name>.pid` data file (JSON, atomically written, freely readable). Fixes Windows PermissionError when the web UI tried to read the worker's pid file while the worker held an `msvcrt.locking()` lock on it. Linux behavior unchanged because `fcntl.flock` is advisory. | Windows runtime bug report. |

Future edits to this FRD must add a row above with the date, the change summary, and the driver.
