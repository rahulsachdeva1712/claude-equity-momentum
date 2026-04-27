# Equity Momentum Rebalance — Functional Requirements Document

Living document. Source of truth going forward.
Last updated: 2026-04-26 (cash-aware sizing + security_id fallback + cost-basis realized).

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
- Membership rule (Champion B, matches the 2-year backtest): BSE main-board equity (`series` in `{A, B, T, X, XT}`) with 20-day average daily volume `>= 10,000 shares`. No market-cap floor (the BSE bhavcopy used in the backtest carries no market-cap data, so Champion B was validated without any mcap gate — the live app matches that universe).
- Membership is refreshed nightly by the worker's universe-refresh job (B.5): it downloads the most recent BSE bhavcopy window, applies the series + ADV filter, joins on ISIN against a cached Dhan scrip-master snapshot to attach `security_id` and `exchange_segment = BSE_EQ`, and atomically writes `<state_dir>/universe/universe.csv`. Strategy and paper/live modules read that file through `CsvUniverseProvider`; a symbol absent from the scrip master is dropped (no `security_id` → cannot place orders).
- A symbol must have daily OHLCV data available to participate.
- A symbol must have the configured ranking metric and weighting metric available for the signal date to remain eligible.
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
| Market-cap threshold | disabled by default (`use_mcap_filter = False`; Champion B match). Value retained in config as `market_cap_min_cr = 100.0` for one-flag re-enable. |
| Universe series filter | `series in {A, B, T, X, XT}` (BSE main-board equity) |
| Universe ADV floor | `adv_20d >= 10,000` shares, computed over the 20-day bhavcopy window |
| Breadth threshold | 0.0, therefore breadth gate disabled |
| Use intraday volume filter | True |
| Intraday volume operator and threshold | `vol_0925_0930 >= 1000` shares |
| Intraday volume window | 09:25–09:30 IST on signal date |
| Signal generation and execution time | 09:30 IST (single consolidated job) |
| Execution model | `next_day_0930` (fill at 09:30 close of the session following the signal date) |
| Explicit transaction cost | 10 basis points |

## A.5 Active Filters
- Universe construction (applied nightly by the refresh job, not per-signal): `series in {A, B, T, X, XT}` and `adv_20d >= 10,000` shares. Symbols failing either never appear in `universe.csv` and so cannot be selected.
- Trend filter 1: `close > EMA(21)`.
- Trend filter 2: `EMA(21) > EMA(50)`.
- Momentum filter: `RSI(14) >= 88`.
- Volatility filter: `ATR(20) percent <= 5 percent`.
- Intraday volume filter: `vol_0925_0930 >= 1000` shares. Evaluated at the 09:30 execution job using the live intraday candles for the current session. A symbol failing this gate is treated as ineligible for that session regardless of its ranking.
- Breadth gate: configured in code but inactive because `breadth_threshold = 0.0`.
- MFI gate: configured in code but inactive because `use_mfi_filter = False`.
- CCI gate: configured in code but inactive because `use_cci_filter = False`.
- Market-cap gate: configured in code (`market_cap_min_cr = 100.0`) but inactive because `use_mcap_filter = False` — Champion B was validated without it. Re-enable by flipping the flag.

## A.6 Entry Criteria
A stock becomes a long candidate for a signal date only if all of the following are true on that date:
- The stock belongs to the `all_bse_equities` universe (series + ADV filter applied nightly per A.2) and has usable daily data.
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
- Target values are computed as `weight × portfolio_value`, where `portfolio_value` is the **current** PV at sizing time — `cash + Σ(qty × marked_price)` for the open paper book — not a hardcoded seed. Live trading sizes off the same PV computation; Dhan independently enforces cash on the order side. Sizing off PV (rather than a fixed notional) means a winning portfolio scales up rebalance-over-rebalance and an underwater one scales down, instead of every rebalance pretending it has a fresh ₹1L. (`app/worker/jobs.py::_capital_for` → `app/paper/engine.py::paper_portfolio_value`.)
- Marked price for the PV computation uses the same fallback chain as `web.views.book_rich`: `live_ltp` → last BUY fill price → `avg_cost`. Fresh entries contribute notional == cost basis (so PV == seed minus charges right after a fill); intraday and overnight refreshes pull from `live_ltp` as the worker polls.
- Target values are converted into integer shares by `floor(target_value × (1 - txn_cost_bps/10000) / execution_price)`. Trading cost is the explicit 10 bps haircut.

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
- The universe is computed from the latest BSE bhavcopy window (see A.2). `adv_20d` is a trailing 20-day average, so a symbol briefly spiking into or out of liquidity can enter or leave the universe one session behind. This matches how the Champion B backtest was constructed.
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

Two OS processes, one SQLite database, one filesystem state directory `~/.claude-equity-momentum/`. Credentials live in `~/Documents/shared/.env` (resolved via `Path.home()`, gitignored, override with `EMRB_ENV_FILE`); the state directory holds only runtime data (db, logs, pid files, artifacts). The shared-folder location lets sibling tools running against the same Dhan account read the same daily token without copy-pasting it into multiple files.

| Process | Role | PID file |
|---|---|---|
| `worker` | Long-running daemon. Owns all Dhan writes. Runs APScheduler, live reconciliation loop, token expiry watcher, paper book maintenance. Single writer to SQLite. | `run/worker.pid` |
| `web` | FastAPI + Jinja + HTMX + Plotly. Renders UI. Reads SQLite. Writes only to the `settings` table (kill switch, worker control) and to `~/Documents/shared/.env` on token update. | `run/web.pid` |

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

- Access token is stored in `~/Documents/shared/.env` under `DHAN_ACCESS_TOKEN=...`. The file lives in the user's shared folder (resolved via `Path.home()` so the same code works on any machine), is gitignored, and is the only place credentials are kept on disk. `EMRB_ENV_FILE` overrides the location for tests or non-default setups.
- Other required secrets: `DHAN_CLIENT_ID`.
- **TOTP secret and PIN are NOT used.** Dhan's official v2 API uses a manually issued access token. Automated TOTP login is unofficial and out of scope.
- User action: generate a fresh access token from the Dhan web portal each morning and paste it into `~/Documents/shared/.env`. The worker watches the file (mtime polled every 10s) and hot-reloads on change.
- On startup the worker validates the token by calling a cheap Dhan read endpoint (e.g., fund limits). Invalid or expired token → worker stays alive, live trading automatically disabled, alert raised, UI shows "Token invalid/expired." Paper trading is unaffected.
- Token expiry is parsed from the JWT `exp` claim. UI top bar shows "Token valid, expires in Xh Ym." 60 minutes before expiry an alert is raised. 10 minutes before expiry a second alert is raised.
- Secrets never logged. `.env` is git-ignored. Redaction layer scrubs tokens from every log line and every `audit_log` row.

## B.5 Market Calendar and Scheduler

- No pre-baked holiday calendar. Market-status was originally sourced from Dhan's `/v2/marketfeed/marketstatus` endpoint, but Dhan retired it with no v2 replacement. The worker now derives OPEN/CLOSED from a local IST weekday + market-hours check (`app.time_utils.is_market_hours`). Downstream safety gates (intraday candle fetch, order placement) fail-close on non-trading weekdays, so a holiday is caught at execution time even though the pill reads `OPEN`.
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
- Market-status poller: every 30 seconds, the worker calls `DhanClient.market_status()` and writes the result to `settings.market_status` with `updated_at = now_ist()`. Since Dhan retired `/v2/marketfeed/marketstatus`, that method now returns `OPEN` during IST weekday market hours (09:15–15:30) and `CLOSED` otherwise; no HTTP call is made. The web process reads this row for the top-bar pill — FRD B.2 forbids web-side Dhan calls, so the worker remains the sole writer. Rows older than 90 seconds (3 × cadence) render as `unknown` on the pill, which now signals "worker down" rather than "Dhan API outage."
- Sizing rule (FRD A.7, B.6): every rebalance sizes target qty against the **current portfolio value**, not a fixed seed notional. PV = `paper_cash` (seed minus net deployed minus charges) + `Σ(qty × marked_price)` for open positions. Marked price uses the same `live_ltp → last BUY fill → avg_cost` fallback chain as `book_rich`. The execution path orders EXIT/TRIM rows before BUY rows in the same session so realised cash funds the BUYs, and each BUY runs through a cash gate that scales qty down (or skips with `insufficient_cash`) when notional + charges exceed available cash. This guards against the 2026-04-26 leverage bug where a full rotation deployed ~₹2L against a ₹1L seed because EXITs failed and BUYs assumed fresh capital.
- Paper MTM refresh job: every minute Mon-Fri (full day window), the worker re-runs `compute_daily_pnl` for the current session date with no explicit fetcher, so the engine pulls marks from `live_ltp` (kept fresh by `ltp_poll_job` during market hours) with a fall-back to last BUY fill price. This keeps `paper_pnl_daily` for today in sync with the open paper book between sessions, including overnight and weekends — the headline KPI tiles and the Trade Log portfolio value update without waiting for the next 09:30 rebalance.
- Command inbox poller: every 2 seconds, the worker scans `run/commands/*.now`. Currently one recognised command — `run_rebalance.now` — which the web process drops in response to the "Run rebalance now" UI button. The worker consumes the file, enforces the B.13 idempotency guard (reject if `sessions.execution_completed_at` is set), discards files older than 300 seconds as stale, and runs `execution_job` out-of-band. All other market-status / kill-switch / token safety gates still apply. Unknown command filenames are logged and removed.
- **18:00 IST — Universe refresh job (Mon-Fri, `misfire_grace_time=3600`).** The worker's `universe_refresh_job` calls `app.universe.refresh.refresh_universe`, which: (1) downloads the BSE bhavcopy for each weekday in the trailing ~35-day window into `<state_dir>/universe/bhavcopy/`, tolerating weekend/holiday 404s and both the modern CSV and legacy ZIP formats; (2) ensures `<state_dir>/universe/scrip_master.csv` is fresh (re-download if >7d old) from Dhan's public scrip master; (3) filters bhavcopy rows to `series in {A, B, T, X, XT}` and computes per-symbol 20-day ADV, keeping rows with `adv_20d >= 10,000`; (4) inner-joins on ISIN to attach `security_id` and `exchange_segment = BSE_EQ`; (5) writes `<state_dir>/universe/universe.csv` atomically (temp + rename); (6) stamps `settings.universe_refresh_at`, `settings.universe_count`, `settings.universe_source_date`. The job never crashes the scheduler — failures raise an `error` alert and the strategy keeps reading the previous artifact. On fresh install (no `universe.csv` present at worker start) the scheduler registers a one-shot bootstrap run 5 seconds after start-up.
- Catch-up behavior: if the worker is not running at 09:30, the job does not run retroactively. On next start, UI shows "Missed today's execution" alert. No auto-catch-up, because both the intraday volume window and the specified 09:30-close fill price are session-time artifacts that cannot be reconstructed later.

## B.6 Paper Trading Module

**Purpose.** Always-on virtual book that applies the strategy identically to live, with two goals: (a) operate when live is switched off, (b) serve as the "shadow" book whose numbers match live within rounding when live is on.

**Inputs.** Daily signal set from the consolidated 09:30 job (same job that produces paper fills in the same run); Dhan historical / 1-minute candle API for the 09:25–09:29 volume candles and the 09:30 fill candle.

**Fill rule.** Every paper order fills at the **close of the 09:30 minute candle** for the corresponding symbol. If Dhan returns no 09:30 candle for a symbol (halt, no trade), that symbol is skipped for that session and an alert is raised — matching the strategy's "no tradable bar" rule in A.9. Note that a symbol cleared the 09:25–09:30 volume gate in A.5 before ever producing a paper order, so a missing 09:30 candle at fill time is the only "no tradable bar" case remaining.

**security_id resolution.** EXIT and TRIM orders generated for held-but-dropped names (a position whose symbol no longer appears in today's signals row) need a `security_id` to fetch the 09:30 candle and to place a live SELL. `app/worker/jobs.py::_resolve_security` walks a fall-back chain — today's signals → most recent prior signals row for that symbol → `universe.csv` — so the executor can always price a SELL it knows it wants to make. Without this fall-back, EXITs silently SKIP with note `no_0930_candle`, the BUYs in the same session deploy on top of the held positions, and the book over-leverages (the 2026-04-26 trigger).

**Execution order and cash gate.** `paper_engine.execute_orders` orders rows so EXIT and TRIM rows fire ahead of BUY rows in the same session (`ORDER BY CASE action WHEN 'EXIT' THEN 0 WHEN 'TRIM' THEN 1 ELSE 2 END`). After the SELL leg runs, each BUY checks `paper_cash(conn)` (seed − net deployed − charges, recomputed from `paper_fills`); if `qty × price + estimated charges` exceeds available cash, qty is scaled down to what fits, or the order is SKIPPED with note `insufficient_cash` and a warn alert when even one share doesn't fit. Combined with the PV-based sizing in A.7, this guarantees the paper book never exceeds the seed plus realised gains — leverage is structurally impossible.

**Charges.** Paper fills apply the full Dhan charge stack for CNC delivery:
- Brokerage (Dhan's publicly posted CNC rate — currently zero for delivery, but computed via a pluggable function so a future change does not require a code change).
- STT / CTT.
- Exchange transaction charges (BSE rate).
- SEBI turnover fee.
- Stamp duty.
- GST on brokerage + transaction + SEBI.

Breakdown persisted per `paper_fills` row so UI can show "Non broker charge" identically to live. The charges function is the **same code path** used by the live module when live fills arrive — both read from one shared `charges.py`.

**Non-broker charges are excluded from headline P&L.** Realized, unrealized, and MTM in `paper_pnl_daily` — and in the per-SELL cells of the Trade Log — are computed **gross** of non-broker charges (STT, exchange txn, SEBI, stamp, GST). Non-broker charges are surfaced as a per-day footer total at the bottom of the Trade Log so the user can still see the drag on net returns, but the strategy-performance headline numbers track pure price gain/loss. Rationale: regulatory charges are price-invariant and drown the v1 paper replay's noise in tiny tiles; the strategy-level question is whether price selection works, not whether statutory fees eat the P&L.

**Realized P&L is cost-basis, not SELL-notional.** `paper_pnl_daily.realized` and the headline Cumulative tile both use `paper.engine._cost_basis_realized_per_session`, which replays the full `paper_fills` history through a running average-cost book and emits per-session realized = `Σ(sell_price - avg_cost_at_time) × qty`. The same average-cost method used by `_apply_to_book` on the live `paper_book` and by `web.views.day_grouped_trade_log` for the per-SELL cells, so the three never disagree. (The previous v1 implementation summed SELL notional which read 0 on a buy-only day but would have read ~99k on a full rotation — wrong by the cost basis.)

**Parity with live.** When live trading is enabled and a live fill lands at quantity `q_live` (possibly a partial fill less than ordered), the paper fill for the same symbol for that session is **adjusted to `q_live`** before MTM is computed. If live is disabled, paper fills at the intended target quantity. This is the "paper follows live quantity" rule.

**Corporate actions.** No adjustments applied to the paper book. If a position exists on record date, the paper quantity stays as-is through the event. Live side is already reconciled from the Dhan positions book so it reflects whatever Dhan applies. This intentionally lets the two drift when a corporate action happens — documented limitation, not a bug.

**MTM refresh.** `compute_daily_pnl` writes today's `paper_pnl_daily` row at the 09:30 execution job and is then re-run **every minute Mon-Fri** by `paper_mtm_refresh_job` (B.5). The refresh path passes no fetcher; the engine builds its own from the chain `live_ltp → last BUY fill price → None`. `live_ltp` is populated minute-by-minute during market hours by `ltp_poll_job`; outside market hours the most recent polled value persists, so today's KPI tiles and the Trade Log portfolio value stay close to the open book overnight and across weekends. (Pre-refresh, the row was frozen at the morning fill snapshot — buy-only days read `today_unrealized = 0` for hours and the trade log's portfolio value stuck at the seed.)

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

**Headline KPI tiles (paper tab, in order).**
1. **Portfolio Value** — `cash + Σ(qty × marked_price)` from `paper_portfolio_value(conn)`. The single number that says "how big is the book right now". Neutral colour, not a delta. Paper-only; live tab omits this tile because live PV would have to come off `live_positions_snapshot`, which is a different code path.
2. **Today's Change** — today's MTM (realized + unrealized for the current session date), green/red.
3. **Today Realized** — today's cost-basis realized P&L. Reads `paper_pnl_daily.realized` for the current session date, written by `_cost_basis_realized_per_session` so it agrees with the per-SELL cells of the Trade Log.
4. **Unrealized P&L** — open-position MTM right now.
5. **Cumulative P&L** — `Σ(realized) + today's_unrealized` across the entire history, using the same shared `_cost_basis_realized_per_session` helper.

A second row of six tiles (Open Positions / Closed Trades / Win Rate / Avg P&L per Closed / Profit Factor / Max Drawdown) summarises the realized-trade distribution.

**Auto-refresh.** Each tab auto-refreshes every 30 seconds when visible (via HTMX polling). Top-bar status polls every 5 seconds.

**No backtest UI.** Per decision #21.

**No trade entry UI.** The user cannot place, modify, or cancel orders manually. All orders originate from the 09:30 job only.

## B.10 Process Management and Stale Process Handling

**Credentials file:** `~/Documents/shared/.env` — gitignored, resolved via `Path.home()` so the same code works on any machine. Override with `EMRB_ENV_FILE` for tests or non-default setups. Shared-folder placement lets sibling tools running against the same Dhan account consume the same daily access token without each maintaining its own copy. The developer edits this by hand to paste the daily Dhan access token. Worker polls mtime every 10s and hot-reloads.

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
- **Idempotency:** the consolidated 09:30 job is **not** idempotent once live orders are sent or paper fills are written; a guard prevents re-run by checking a `sessions.execution_completed` flag. A manual "rerun" command from `run/commands/` is rejected with an alert after this flag is set for the session date. The UI "Run rebalance now" button is implemented as a sentinel drop at `run/commands/run_rebalance.now` — the web process does not call the execution path directly (preserves the single-writer invariant).
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
| 2026-04-24 | Wired the "Run rebalance now" manual trigger. UI gains a button on both tabs; `POST /actions/run-rebalance` touches `run/commands/run_rebalance.now` and redirects back. New worker job `command_inbox_job` polls the inbox every 2 s, enforces the B.13 idempotency guard (rejects with alert if `sessions.execution_completed_at` is already set), discards sentinels older than 300 s, runs `execution_job` out-of-band, and deletes the file before invoking the job so crashes don't loop-retry. Unknown filenames are logged + removed. Web process never calls the execution path directly (preserves B.2 single-writer rule). Updated B.5 (new poller), B.13 (idempotency + manual-trigger note), added `tests/test_command_inbox.py`. Full suite 102/102 green. | User-reported: no way to trigger a rebalance off-schedule for testing, and today's 09:30 slot had been missed because the worker was down. |
| 2026-04-24 | Wired the dynamic universe refresh so live/paper trade the exact universe the Champion B backtest was validated on. New `app/universe/` package (`bhavcopy.py`, `scrip_master.py`, `refresh.py`) downloads the BSE bhavcopy window, caches Dhan's scrip master, applies `series in {A,B,T,X,XT}` + `adv_20d >= 10,000`, joins on ISIN to attach `security_id`, and atomically writes `<state_dir>/universe/universe.csv`. `CsvUniverseProvider` now reads that path (schema gained `isin`, `sc_code`; legacy 4-column CSVs still load). Scheduler registers a nightly 18:00 IST run (Mon-Fri) plus a 5-second bootstrap one-shot when the artifact is missing. Added `StrategyConfig.use_mcap_filter` (default `False` to match Champion B; `market_cap_min_cr` retained for one-flag re-enable); `_static_eligible_mask` and `compute_universe_metrics` now gate the mcap check on this flag, and the panel no longer needs a `market_cap_cr` column when the gate is off. Updated A.2, A.4, A.5, A.6, A.11, B.5. New tests: `tests/test_universe_bhavcopy.py`, `tests/test_universe_scrip_master.py`, `tests/test_universe_refresh.py`, `tests/test_universe_provider.py`; strategy tests updated to exercise both default-off and opted-in mcap-gate behavior. Full suite 138/138 green. | Close the loop on the Champion B rollout: backtest used a bhavcopy-derived universe (no mcap filter, series+ADV only), live app was still pointing at a hand-written CSV with a 100-cr mcap floor, so the two universes disagreed. |
| 2026-04-24 | Excluded non-broker charges (STT, exchange txn, SEBI, stamp, GST) from headline P&L. `app/paper/engine.py::compute_daily_pnl` no longer subtracts `charges_total` from realized; `app/web/views.py::_summary`, `_replay_closed_trades`, and `day_grouped_trade_log` match. Per-day non-broker charge total now surfaces as a `<tfoot>` row in the Trade Log partial so the drag is still visible. `paper_pnl_daily` rows written before this change can be recomputed by re-running the day's rebalance via `backfill_today`. Updated B.6 with the rationale; `tests/test_paper_engine.py::test_pnl_realized_plus_unrealized` flipped to assert realized == 0 on a BUY-only day. Full suite 144/144 green. | User feedback: tiny regulatory charges muddied the strategy-level read on the Paper tab; headline should track pure price selection with charges visible separately. |
| 2026-04-24 | Dhan retired `/v2/marketfeed/marketstatus` (started returning 404); the v2 docs no longer list any replacement endpoint. `DhanClient.market_status()` now computes OPEN/CLOSED locally from an IST weekday + market-hours check (`is_market_hours()`), with no HTTP call. `_PATHS["market_status"]` removed. The `market_status_poll_job` keeps its 30 s cadence and continues to write to `settings.market_status`; a stale row still falls back to `unknown` on the pill (now meaning "worker down" rather than "Dhan outage"). Holidays that fall on weekdays will read `OPEN` here, but downstream safety gates (intraday candle fetch, order placement) fail-close on non-trading days, so the blast radius is limited to wasted fetches. B.5 updated; `tests/test_market_status_job.py` and `tests/test_dhan_client.py::test_market_status_*` rewritten to freeze IST time instead of mocking HTTP. | Dhan retired the market-status endpoint; the poller was 404-ing every 30 s, leaving the top-bar pill stuck at `unknown`. |
| 2026-04-26 | Trade Log per-fill `kind` label was hardcoded — every SELL read "Partial Exit" and every BUY read "New Entry", regardless of whether the fill actually closed the position or topped up an existing leg. Fixed by replaying the running cost-basis book in `views.day_grouped_trade_log` and tagging each fill: SELL closing to qty=0 → "Full Exit", SELL leaving residual → "Partial Exit", BUY with no prior position → "New Entry", BUY adding to one → "Top-up". Template badge class now keys off `r.side` instead of `r.kind == "New Entry"` so "Top-up" still renders with the buy-side badge. New regression: `tests/test_trade_log_pv.py::test_kind_label_distinguishes_full_vs_partial_exit_and_top_up`. | User feedback: "why does it say partial exit?" — three full exits in the 27-Apr cleanup were rendering as "PARTIAL EXIT" even though they took the position to zero. |
| 2026-04-26 | Added a "Portfolio Value" tile to the paper-tab KPI row (first position). Sources `summary.portfolio_value` from `paper_portfolio_value(conn)` (cash + Σ(qty × marked_price)). Live tab omits it — a live PV tile would need to read from the broker snapshot, which is a different code path; the template uses an `{% if summary.portfolio_value is defined %}` guard so live stays clean. Tests: `tests/test_paper_engine.py::test_summary_includes_portfolio_value_for_paper`, `test_summary_omits_portfolio_value_for_live`. | User request: surface PV alongside the four delta tiles. |
| 2026-04-26 | Cash-aware sizing + security_id fallback chain + cost-basis realized — fixes a serious leverage bug where a 27-Apr rebalance from "fully invested in 3 names" to "1 new name" left the paper book at 2× notional. Three coupled changes: **(1) Sizing.** `app/worker/jobs.py::_capital_for` no longer returns a hardcoded ₹1L (or `settings.capital`); it now returns the live portfolio value computed by `app/paper/engine.py::paper_portfolio_value` = `paper_cash(seed minus net deployed minus charges) + sum(qty × marked_price)` where marked uses the same `live_ltp → last BUY → avg_cost` fallback chain as `web.views.book_rich`. So every rebalance sizes against actual current PV, not a fresh seed assumption. **(2) Execution discipline.** `paper_engine.execute_orders` now `ORDER BY` puts EXIT/TRIM ahead of BUY in the same session so realised cash funds the BUY leg, and each BUY runs through a cash gate: if `qty × price + charges` exceeds `paper_cash(conn)` at fill time, the qty is scaled down to what fits (or skipped with an `insufficient_cash` warn alert if not even one share is affordable). Live trading is unaffected by the gate — Dhan enforces cash directly — but live still benefits from the PV-based sizing. **(3) security_id resolution.** New `app/worker/jobs.py::_resolve_security` does today's signals → most recent prior signals → `universe.csv`, used by both `_fetch_0930_closes` and the `paper_orders` fan-out that feeds `place_orders`. Held-but-dropped names (an EXIT diff target whose row no longer exists in today's signals) now price and place correctly — the original 2026-04-26 trigger where 3 EXITs were silently SKIPPED with note `no_0930_candle`. **Bonus, related fix:** `paper_pnl_daily.realized` and the headline Cumulative tile previously used a v1 SELL-notional approximation; with no SELLs in the book that read as 0 and was tolerable, but the moment a real EXIT fired the tile would have shown gross sale notional (~₹99k for a full rotation) instead of the actual ~₹1k cost-basis P&L. Both code paths now share `paper.engine._cost_basis_realized_per_session` which replays the full fills history through a running average-cost book — same method `_apply_to_book` uses on the live `paper_book` and the same method `views.day_grouped_trade_log` uses, so the three never disagree. New tests: `tests/test_paper_engine.py` (+5 cases — cash math, PV math, scaled BUY, skipped BUY, SELL-first order); `tests/test_resolve_security.py` (5 cases). Suite 160/160. **One-time data fix applied to the live state DB:** backfilled the 3 missing 27-Apr SELL fills (ADANIPOWER 172 @ 213.05, MEERA 394 @ 66.98, WELCORP 30 @ 1208.25) at the most recent daily close, removed those rows from `paper_book`, and re-ran `compute_daily_pnl` so `paper_pnl_daily` reflects the corrected realized P&L. Cash went from -98,619 (impossible negative) to +559.83. | User feedback: "the portfolio only had 100000 rupees, but it again invested 100000 today without exiting the previous bought stocks while rebalancing? do you see any problem in that?" → "always check on the portfolio value how much you can buy or rebalance else this would be repeated next time". |
| 2026-04-26 | Headline KPI tiles and the Trade Log portfolio value used to freeze at the morning fill snapshot — `compute_daily_pnl` ran once at 09:30, and `paper_pnl_daily` was never refreshed between sessions. A buy-only day showed `today_change = today_unrealized = 0` and PV stuck at the ₹1L seed because the trade log's PV math was `seed + cumulative_realized` only. Three changes: (1) `app/paper/engine.py::compute_daily_pnl` now accepts an optional `ltp_fetcher`; when omitted it builds the same fallback chain as `web.views.book_rich` (`live_ltp` → last BUY fill price → unknown), so callers without a fresh same-day fetcher still get a sensible mark. (2) New `paper_mtm_refresh_job` (B.5) re-runs `compute_daily_pnl(sess)` once per minute Mon-Fri across the full day window, keeping today's `paper_pnl_daily.unrealized` in sync with the open book and surviving overnight / weekend gaps via `live_ltp`'s last polled candle. (3) `web.views.day_grouped_trade_log` PV math is now `seed + cumulative_realized + unrealized_eod[session]`, where `unrealized_eod` is sourced from `paper_pnl_daily`. Tests added: `tests/test_paper_engine.py::test_pnl_uses_live_ltp_when_no_fetcher_passed`, `test_pnl_falls_back_to_last_buy_when_live_ltp_empty`, and `tests/test_trade_log_pv.py` (3 cases). `paper_pnl_daily.realized` retains its v1 SELL-notional approximation; per-day cost-basis P&L is still reconstructed by the trade-log replay loop (and by `running_pnl_to_end_of_day` in the PV math), so realized stays consistent. | User feedback: "the portfolio value is still showing 100000, why? also the top KPIs are not populating?" |
| 2026-04-26 | Removed the alerts dropdown pill from the top bar (`app/web/templates/partials/top_bar.html`). The pill was an implementation extra that B.9 never specified — the spec keeps alerts in a settings modal (line 322) and the unread-count badge on the settings icon (B.11). The `alerts` table, `/alerts/{id}/ack` endpoint, and worker-side alert writers are unchanged; only the top-bar surface is gone. | User feedback: alerts pill cluttered the top bar and wasn't being acted on from there. |
| 2026-04-25 | Relocated `.env` from `<repo>/.env` to `~/Documents/shared/.env`. Path resolved via `Path.home() / "Documents" / "shared" / ".env"` so the same code works on any machine the repo is checked out on; `EMRB_ENV_FILE` overrides the location for tests and non-default setups. `app/paths.py::env_file()` rewritten; `app/settings.py` docstring updated; `run.bat` now seeds `%USERPROFILE%\Documents\shared\.env` and `run.sh` seeds `$HOME/Documents/shared/.env` (with `chmod 600`); `tests/test_settings_env.py` fixture uses `monkeypatch.setenv("EMRB_ENV_FILE", ...)` instead of patching `project_root`. README + B.2 / B.4 / B.9 / B.10 updated to match. Existing repo-root `.env` was migrated by hand. | User request: one shared credentials file consumable by sibling tools running against the same Dhan account, so pasting a fresh access token each morning updates every consumer at once. |

Future edits to this FRD must add a row above with the date, the change summary, and the driver.
