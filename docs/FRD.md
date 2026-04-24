# Equity Momentum Rebalance — Functional Requirements Document

Living document. Source of truth going forward.
Last updated: 2026-04-24 (Champion B baseline rollout).

This FRD has two parts:
- **Part A — Strategy Specification.** Originally imported verbatim from `Equity_Momentum_App_FRD.docx` (dated 2026-04-22). Amended in-place since then; see B.16 Change Log for each strategy-rule change and its driver. Describes the trading rules only.
- **Part B — Application Functional Requirements.** Covers everything the strategy doc intentionally excluded: paper module, live module, Dhan integration, reconciliation, scheduling, UI, processes, persistence, failure handling.

---

# Part A — Strategy Specification

Equity Momentum Rebalance Strategy. Originally code-derived from the repository snapshot on 2026-04-22; subsequently amended to reflect updated strategy rules (see B.16 Change Log). This document is limited to the strategy itself: universe, inputs, filters, entry rules, exit rules, position sizing, and rebalance criteria. It intentionally avoids broader application, deployment, and backtest-operating details. Timing references are included only where they affect paper and live trading behavior.

## A.1 Strategy Overview
- Strategy type: long-only daily momentum strategy.
- Market: BSE equities only.
- Configured universe: `all_bse_equities`.
- Portfolio style: diversified momentum portfolio with a maximum of 20 holdings.
- Selection approach: eligible stocks are ranked by 3-month relative return (`relative_return_63d`) and sized by inverse ATR percent (lower-volatility leaders get larger weights). The volume-liquidity filter (A.5) is applied **before** ranking, so the top-20 is selected from the volume-qualified set only.
- Current execution assumption for paper and live workflow: a single daily job at 09:30 IST computes the signal using completed D-1 close data plus the 09:25–09:30 intraday volume window, then executes in the same run. There is no separate pre-market signal step.

## A.2 Universe Definition
- Universe name in code: `all_bse_equities`.
- Exchange scope: BSE only.
- A symbol must have daily OHLCV data available to participate.
- A symbol must have the configured ranking metric and weighting metric available for the signal date to remain eligible.
- A symbol must have a usable `market_cap_cr` value to pass the active market-cap filter.
- A symbol must have 09:25–09:30 IST one-minute candle data available on the signal date to evaluate the intraday volume filter.

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
- Intraday volume metric `vol_0925_0930`: total traded volume, in shares, over the five one-minute candles ending at 09:30 IST on the signal date (i.e., the sum of the 09:25, 09:26, 09:27, 09:28, and 09:29 candles). Used only as a live-liquidity gate at execution time.

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
| RSI operator and threshold | `>= 88.0` |
| MFI operator and threshold | `>= 70.0`, currently inactive |
| CCI operator and threshold | `>= 110.0`, currently inactive |
| ATR operator and threshold | `<= 0.05` |
| Price vs EMA operator | `close > EMA(21)` |
| EMA stack operator | `EMA(21) > EMA(50)` |
| Sort metric | `relative_return_63d`, descending |
| Weight metric | `relative_return_252d` (used only when `weight_scheme = "rel"`) |
| Weight scheme | `inv_atr` (weight proportional to `1 / atr_pct`) |
| Maximum positions | 20 |
| Minimum positions | 1 |
| Full rebalance | True |
| Market-cap threshold | 100.0 crore |
| Breadth threshold | 0.0, therefore breadth gate disabled |
| Use intraday volume filter | True |
| Intraday volume operator and threshold | `vol_0925_0930 >= 1000` shares |
| Intraday volume window | 09:25–09:30 IST on signal date |
| Signal generation and execution time | 09:30 IST (single consolidated job) |
| Execution model | `next_day_0930` (fill at 09:30 close of the session following the signal date) |
| Explicit transaction cost | 10 basis points |

## A.5 Active Filters
- Market-cap filter: `market_cap_cr >= 100.0`.
- Trend filter 1: `close > EMA(21)`.
- Trend filter 2: `EMA(21) > EMA(50)`.
- Momentum filter: `RSI(14) >= 88`.
- Volatility filter: `ATR(20) percent <= 5 percent`.
- Intraday volume filter: `vol_0925_0930 >= 1000` shares. Evaluated at the 09:30 execution job using the live intraday candles for the current session. A symbol failing this gate is treated as ineligible for that session regardless of its ranking.
- Breadth gate: configured in code but inactive because `breadth_threshold = 0.0`.
- MFI gate: configured in code but inactive because `use_mfi_filter = False`.
- CCI gate: configured in code but inactive because `use_cci_filter = False`.

## A.6 Entry Criteria
A stock becomes a long candidate for a signal date only if all of the following are true on that date:
- The stock belongs to the `all_bse_equities` universe and has usable daily data.
- `market_cap_cr >= 100.0`.
- `close > EMA(21)`.
- `EMA(21) > EMA(50)`.
- `RSI(14) >= 88`.
- `ATR(20) percent <= 5 percent`.
- `vol_0925_0930 >= 1000` shares on the current session (evaluated at 09:30 IST).
- The configured sort metric `relative_return_63d` is present.
- The configured weight metric `relative_return_252d` is present.

After eligibility is established — **including the 09:25–09:30 volume gate** — all surviving names are ranked by `relative_return_63d` in descending order, and only the top 20 names are selected. Because the volume gate is part of eligibility rather than a post-selection filter, a symbol that ranks highly on momentum but fails volume is never in the top 20 to begin with; the slot goes to the next volume-qualified name. If fewer than 1 selected name remains, no portfolio is formed for that signal date.

## A.7 Position Sizing Rules
- Selected names are weighted under the configured `weight_scheme`. The current baseline is `inv_atr`: each target weight is proportional to `1 / atr_pct`, so lower-volatility leaders carry larger allocations. `atr_pct` is floored at `0.001` before inversion to prevent a zero-ATR name from dominating.
- Alternative schemes are retained in code for experimentation: `rel` (proportional to `relative_return_252d`, the legacy pre-Champion-B behavior), `rel_rank` (proportional to `relative_return_63d`), and `equal` (`1/N`).
- For `rel` and `rel_rank`, negative contributors are clipped to zero before normalization. If the clipped sum is non-positive, all selected names receive equal weight.
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
- The strategy runs once per session, at 09:30 IST, as a single consolidated job.
- Daily indicators and relative-return metrics are computed from completed D-1 close data.
- The 09:25–09:30 intraday volume gate is evaluated using the current session's one-minute candles at the moment the job runs.
- Paper and live trading consume the same 09:30 target set: what to buy, what to trim, what to hold, and what to exit. There is no intermediate 09:10 recommendation step; the entire target set is produced and acted on at 09:30.

## A.11 Strategy Constraints And Known Limitations
- The strategy is long-only.
- The strategy is daily only and does not include intraday entry logic.
- There is no hard stop-loss, take-profit, trailing stop, or hedge overlay in the strategy code.
- The market-cap filter uses current-state market-cap data rather than point-in-time historical market-cap snapshots.
- Integer-share sizing and trading-cost deductions can cause actual realized weights to differ from ideal target weights.
- The `next_day_0930` execution model used for offline strategy validation matches live semantics (signal on D close, fill at D+1 09:30 close). When the validation harness is driven by daily bhavcopy data only — which has no intraday prices — the fill is approximated by the D+1 open. Typical open-vs-09:30 slippage on liquid BSE names is a few tens of bps; this is a documented approximation, not a property of the strategy.

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

Both modules consume the identical 09:30 signal set produced by the consolidated strategy + execution job. There is no backtest UI in the application. Historical data access remains only to compute the indicators and relative-return metrics the strategy needs, plus the intraday 09:25–09:30 one-minute candles used by the volume gate.

UI exposes exactly two tabs: **Paper Trading** and **Live Trading**. A persistent top bar provides global status and a settings modal.

## B.2 Process Topology

Two OS processes, one SQLite database, one filesystem state directory `~/.claude-equity-momentum/`. Credentials live in `<repo>/.env` at the project root (gitignored); the state directory holds only runtime data (db, logs, pid files, artifacts).

| Process | Role | PID file |
|---|---|---|
| `worker` | Long-running daemon. Owns all Dhan writes. Runs APScheduler, live reconciliation loop, token expiry watcher, paper book maintenance. Single writer to SQLite. | `run/worker.pid` |
| `web` | FastAPI + Jinja + HTMX + Plotly. Renders UI. Reads SQLite. Writes only to the `settings` table (kill switch, worker control) and to `<repo>/.env` on token update. | `run/web.pid` |

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

Historical OHLCV is **not** persisted. Each 09:30 run fetches the required daily lookback window plus the 09:25–09:29 intraday candles from Dhan and computes indicators and the volume metric in memory. This avoids stale-cache risk.

## B.4 Authentication and Credentials

- Access token is stored in `<repo>/.env` under `DHAN_ACCESS_TOKEN=...`. The file lives at the project root, is gitignored, and is the only place credentials are kept on disk.
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
  - **09:30 IST — Signal + Execution job (single consolidated run).** Query Dhan market-status. If closed, log and exit. Otherwise:
    1. Fetch required daily OHLCV lookback and compute indicators, absolute returns, and relative returns from D-1 close data.
    2. Apply the static eligibility filters from A.5 (market cap, trend, RSI, ATR).
    3. Fetch the five 09:25–09:29 one-minute candles for each surviving candidate from Dhan, sum traded volume to compute `vol_0925_0930`, and drop any symbol with `vol_0925_0930 < 1000`.
    4. Rank the remaining volume-qualified set by `relative_return_63d` descending; take the top 20; compute target weights under the configured `weight_scheme` (baseline: `inv_atr`, proportional to `1 / atr_pct`).
    5. Write `signals` rows for the session.
    6. Diff against current `paper_book` to produce paper orders (buy/trim/exit). Fetch the 09:30 one-minute candle close for each traded symbol and record `paper_fills` with the full Dhan charge stack; update `paper_book`.
    7. If live trading is enabled (kill switch off), for each live order, place a CNC market order on Dhan tagged with `correlation_id`; subscribe to order status; on fill, update `live_fills` and **adjust the matching `paper_fills` qty to match the actual live fill qty** (the parity rule). On reject, skip the symbol, raise an alert, do not retry.

  The 09:10 pre-market signal job no longer exists. All signal computation happens inside this 09:30 job because the intraday volume gate is a same-session measurement; there is no stable, pre-market form of the target set to surface.
- Live reconciliation loop: every 15 seconds during market hours (09:15–15:30 IST), the worker pulls Dhan positions and order book, filters to our `correlation_id` tags, updates `live_positions_snapshot` and `live_pnl_daily`. Outside market hours the loop is idle.
- Token expiry watcher: every 60 seconds, re-parse `exp`, raise alerts at the 60-min and 10-min thresholds, disable live trading if expired.
- Market-status poller: every 30 seconds, the worker queries Dhan `/marketfeed/marketstatus` and writes the uppercased value to `settings.market_status` with `updated_at = now_ist()`. Transport errors are swallowed so the row's `updated_at` naturally ages out. The web process reads this row for the top-bar pill — FRD B.2 forbids web-side Dhan calls, so the worker is the sole writer. Rows older than 90 seconds (3 × cadence) render as `unknown` on the pill.
- Catch-up behavior: if the worker is not running at 09:30, the job does not run retroactively. On next start, UI shows "Missed today's execution" alert. No auto-catch-up, because both the intraday volume window and the specified 09:30-close fill price are session-time artifacts that cannot be reconstructed later.

## B.6 Paper Trading Module

**Purpose.** Always-on virtual book that applies the strategy identically to live, with two goals: (a) operate when live is switched off, (b) serve as the "shadow" book whose numbers match live within rounding when live is on.

**Inputs.** Daily signal set from the consolidated 09:30 job (same job that produces paper fills in the same run); Dhan historical / 1-minute candle API for the 09:25–09:29 volume candles and the 09:30 fill candle.

**Fill rule.** Every paper order fills at the **close of the 09:30 minute candle** for the corresponding symbol. If Dhan returns no 09:30 candle for a symbol (halt, no trade), that symbol is skipped for that session and an alert is raised — matching the strategy's "no tradable bar" rule in A.9. Note that a symbol cleared the 09:25–09:30 volume gate in A.5 before ever producing a paper order, so a missing 09:30 candle at fill time is the only "no tradable bar" case remaining.

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

**Rejects.** If Dhan rejects an order, the worker logs the reject reason, raises an alert with symbol + reason, and **does not retry**. The matching paper fill also does not happen for that symbol that session (parity). The symbol is effectively absent from the portfolio for that session; the next session's 09:30 job decides the next action.

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
1. **Paper Trading** — mirrors the mockup: Today's summary cards, Signals For Today, Today Status, Daily Paper P&L chart, Cumulative Paper P&L chart, Performance Summary, Current Paper Book, Trade log. "Signals For Today" is empty/"Pending 09:30 run" until the 09:30 job completes; there is no pre-market recommendation list because the volume gate is a 09:30 measurement.
2. **Live Trading** — same visual structure as Paper, but numbers come from the reconciled live dataset. When `live_enabled = false`, the tab shows all historical tagged live activity (if any) plus a prominent banner: "Live trading is OFF. Showing held positions only."

**Top bar (persistent, above tabs).**
- App title on the left.
- Status pills on the right:
  - Worker status: `running` / `stopped` / `stale-cleaned`.
  - Dhan token: `valid until HH:MM IST (Xh Ym)` / `expires in X min at HH:MM IST` / `expired at HH:MM IST <day> (Xh Ym ago)`. Day phrasing collapses to "today" / "yesterday" / weekday name / ISO date so the user can self-diagnose stale-paste errors without decoding the JWT.
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

**Credentials file:** `<repo>/.env` — at the project root, gitignored. The developer edits this by hand to paste the daily Dhan access token. Worker polls mtime every 10s and hot-reloads.

**State directory:** `~/.claude-equity-momentum/` — all runtime state. Kept outside the repo so it survives `git clean` and stays out of `git status`.
```
~/.claude-equity-momentum/
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
    last_signal_<date>.json   (debug snapshot of each 09:30 job)
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

**Windows lock-acquisition hang protection.** On Windows, `msvcrt.locking(fd, LK_NBLCK, 1)` is documented as non-blocking but in practice retries ~10 times with 1-second sleeps before raising; separately, `os.open()` on a stale `.lock` file whose handle was inherited by the OS from a force-closed predecessor can itself block. To keep a forcibly-closed worker's residue from silently stalling the next start, the open+lock step runs in a worker thread bounded by `pidfile.LOCK_ACQUIRE_TIMEOUT_S` (default 15 s). Exceeding the ceiling is surfaced as `AlreadyRunning` with a clear log line rather than a silent multi-minute hang. Each stage of `acquire()` (stale check, unlink, open+lock, pid-file write) is also instrumented at DEBUG so that any future hang is diagnosable from the log alone. If the stale-cleanup unlink of the `.lock` sentinel fails with `PermissionError` (kernel handle still alive), that's logged as a WARNING rather than swallowed silently.

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
- **Idempotency:** the consolidated 09:30 job is **not** idempotent once live orders are sent or paper fills are written; a guard prevents re-run by checking a `sessions.execution_completed` flag. A manual "rerun" command from `run/commands/` is rejected with an alert after this flag is set for the session date.
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
- **09:25–09:29 candle availability at 09:30.** Assumes the five one-minute candles ending at 09:30 (the 09:25, 09:26, 09:27, 09:28, and 09:29 candles) are retrievable from Dhan intraday data at or shortly after 09:30:00 IST for every universe symbol. If a symbol has missing candles across this window, `vol_0925_0930` is treated as 0 and the symbol fails the volume gate. The 60-second latency budget in B.13 accommodates this fetch before live order placement.
- **Session-date definition.** For a signal generated on date `D` at 09:30 using `D-1` close data and `D` 09:25–09:29 intraday candles, the session date is `D` across all tables. All timestamps stored in UTC; displayed in IST.
- **Clock sync.** Assumes host clock is within 2 seconds of true IST. No NTP enforcement in-app.

## B.16 Change Log

| Date | Change | Driven by |
|---|---|---|
| 2026-04-22 | Initial Part B added covering paper, live, Dhan integration, scheduler, processes, UI, data model. | Requirements discussion. |
| 2026-04-22 | Added one-click launchers `run.bat` / `run.sh` and stoppers `stop.bat` / `stop.sh`. First run creates venv, installs deps, seeds `.env`; subsequent runs start worker + web and open the UI. Stoppers send SIGTERM so PID files are cleaned per B.10; forced kills are recovered on next start by the stale-PID cleanup. | User request. |
| 2026-04-23 | Split PID file into a `<name>.lock` sentinel (holds OS exclusive lock) and a `<name>.pid` data file (JSON, atomically written, freely readable). Fixes Windows PermissionError when the web UI tried to read the worker's pid file while the worker held an `msvcrt.locking()` lock on it. Linux behavior unchanged because `fcntl.flock` is advisory. | Windows runtime bug report. |
| 2026-04-23 | `.env` now read with `utf-8-sig` so a UTF-8 BOM (added by Notepad on Windows) is stripped instead of fusing into the first key name. Top-bar token classifier now distinguishes "missing" vs "invalid" vs "expiring" vs "valid"; the invalid label includes a hint about BOM/quotes. | Windows runtime bug: token entered into `.env` was reported as "no token" by the UI. |
| 2026-04-23 | Added an intraday liquidity filter: `vol_0925_0930 >= 1000` shares, measured as the sum of traded volume across the 09:25, 09:26, 09:27, 09:28, and 09:29 one-minute candles on the signal date (A.3, A.4, A.5, A.6). The volume gate is part of eligibility, so the top-5 is ranked from the volume-qualified set only. Dropped the separate 09:10 pre-market signal job; signal computation and execution are now a single consolidated 09:30 IST job (A.1, A.10, B.5, B.6, B.9, B.13). Catch-up rule simplified to a single "missed 09:30 execution" alert, and idempotency tightened to one `sessions.execution_completed` guard. | User requirement: liquidity guarantee at trade time; consequent removal of the pre-market recommendation view. |
| 2026-04-23 | Changed `execution_model` in A.4 from `next_day_open` to `next_day_0930` so the strategy validation model aligns with live fill semantics (D+1 09:30 close, not D+1 open). Added an A.11 note that a daily-bhavcopy-driven validation harness approximates the D+1 09:30 fill with the D+1 open, since bhavcopy carries no intraday prices. | 2-year offline validation run using BSE bhavcopy data as the source. |
| 2026-04-24 | Top-bar token pill now embeds the expiry clock time and a day reference in every state. Expired now reads `expired at HH:MM IST <day> (Xh Ym ago)` instead of bare `expired`; valid reads `valid until HH:MM IST (Xh Ym)`; expiring includes the same `at HH:MM IST` clock. Day reference resolves to "today" / "yesterday" / weekday name / ISO date relative to today's IST date. `_classify_token` gained an optional `now` parameter for deterministic tests. (B.9.) | UX: when a freshly pasted token is rejected, the prior `expired` label was ambiguous between "yesterday's token left in `.env`" and "today's paste failed parsing"; embedding the expiry timestamp lets the user self-diagnose without decoding the JWT. |
| 2026-04-24 | Rolled Champion B baseline: `rsi_threshold` 75 → 88, `atr_pct_max` 0.04 → 0.05, `sort_metric` `relative_return_126d` → `relative_return_63d`, `max_positions` 5 → 20. Added `weight_scheme` config field; default flipped from the legacy `rel` (weights proportional to `relative_return_252d`) to `inv_atr` (weights proportional to `1 / atr_pct`). Updated A.1, A.4, A.5, A.6, A.7, B.5 to match. DB column `signals.rank_by_126d` keeps its legacy name (no migration); the value is now the rank position under `relative_return_63d`. Driven by the 2-year offline sweep (`research/backtest_2y/sweep3.py` + `verify.py`): Champion B posts 8833 percent / 91.7 percent monthly win / Sharpe 12.2 / MDD -7.7 percent over 2y, walk-forward IS 885 percent → OOS 841 percent at the same win rate, vs. 3354 percent for the old baseline stack. | 2-year offline sweep and walk-forward verification. |
| 2026-04-24 | `PidFile.acquire()` now runs `os.open` + `msvcrt.locking(LK_NBLCK)` in a worker thread bounded by `LOCK_ACQUIRE_TIMEOUT_S` (15 s) and converts timeouts into a clean `AlreadyRunning`. Added DEBUG breadcrumbs around each stage (stale check, unlink, open+lock, pid-file write) so future hangs are diagnosable. `_safe_unlink` now returns a bool and logs a WARNING when the `.lock` sentinel cannot be removed (opt-in via `warn_on_busy`), instead of silently swallowing. Updated B.10. | Windows runtime bug: after a force-closed `emrb-worker` window, the next worker start hung in userland with zero log output (0.64 s CPU over 3 min) because stale kernel handles on `run/worker.lock` stalled `os.open`/`LK_NBLCK` and the old silent `PermissionError` swallow hid it. |
| 2026-04-24 | Moved `.env` from the state directory (`~/.claude-equity-momentum/.env`) to the project root (`<repo>/.env`, still gitignored). Runtime state — SQLite db, logs, pid files, artifacts — remains under the state directory. `app/paths.py` gains `project_root()` and `env_file()` now returns `project_root() / '.env'`; `app/settings.py`, `run.bat`, `run.sh`, `README.md`, and B.2 / B.4 / B.10 updated to match. Rationale: users edit `.env` by hand to paste the daily Dhan access token, so keeping it next to the source tree makes it discoverable in an IDE and matches the convention most Python projects follow. | User-reported confusion: token pasted into `<repo>/.env` was not being read because the code looked at the state-dir `.env`. |
| 2026-04-24 | Wired the top-bar market-status pill. A new `market_status_poll_job` runs every 30 s in the worker, calls Dhan `/marketfeed/marketstatus`, and writes the uppercased result to the `settings` table (`key='market_status'`, `updated_at = now_ist()`). The web process reads this row via `views._read_market_status(conn)` and renders the value on the pill; rows older than 90 s (3 × cadence) or transport errors fall back to `unknown`. No Dhan calls from the web side (preserves the B.2 single-writer rule). Added `app/web/main.py` wiring, `tests/test_market_status_job.py`, and top-bar rendering tests in `tests/test_web.py`. B.5 updated with the new poll cadence. Also fixed `tests/test_settings_env.py` (stale after the `.env` relocation) so the full suite is green. | User-reported: top-bar pill always showed `market: unknown` because the previous scaffold passed a hardcoded `None`. |

Future edits to this FRD must add a row above with the date, the change summary, and the driver.
