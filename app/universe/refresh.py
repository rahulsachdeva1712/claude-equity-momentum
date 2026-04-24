"""Daily universe refresh — the bridge between the Champion B backtest and
the live app.

Steps (matching ``research/backtest_2y/verify.py``'s ``BASE2`` settings):
1. Download yesterday's BSE bhavcopy plus ~30 trading days back to compute ADV.
2. Ensure the Dhan scrip-master snapshot is cached (weekly refresh).
3. Filter bhavcopy rows to main-board equity: ``series in {A, B, T, X, XT}``.
4. Compute 20-day average daily volume per symbol, keep rows with ADV >= 10_000.
5. Join on ISIN against the scrip master to pick up ``security_id`` +
   ``exchange_segment=BSE_EQ``.
6. Write ``<state_dir>/universe/universe.csv`` with columns:
   ``symbol, security_id, exchange_segment, market_cap_cr, isin, sc_code,
   series, adv_20d``. ``market_cap_cr`` is left blank (see FRD A.4 — the
   market-cap gate is off in Champion B config).
7. Stamp ``settings.universe_refresh_at`` and ``settings.universe_count``.

The emitted CSV is the single contract consumed by
``app.strategy.universe.CsvUniverseProvider``.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import sqlite3
from pathlib import Path

import pandas as pd

from app.db import connect, tx
from app.paths import state_dir
from app.time_utils import now_ist
from app.universe.bhavcopy import load_recent_bhavcopies
from app.universe.scrip_master import ScripMasterError, fetch_scrip_master, load_bse_equities

log = logging.getLogger("universe.refresh")


# Champion B universe parameters. Source: research/backtest_2y/backtest.py
# (EQUITY_SERIES, ADV_MIN) + verify.py (turn_min=0.0 for the non-degraded run).
EQUITY_SERIES: frozenset[str] = frozenset({"A", "B", "T", "X", "XT"})
ADV_MIN_SHARES: int = 10_000
ADV_LOOKBACK_DAYS: int = 20

SETTINGS_KEY_UNIVERSE_REFRESH_AT = "universe_refresh_at"
SETTINGS_KEY_UNIVERSE_COUNT = "universe_count"
SETTINGS_KEY_UNIVERSE_SOURCE_DATE = "universe_source_date"

# Fixed CSV column order so the provider contract is explicit.
UNIVERSE_CSV_COLUMNS = [
    "symbol",
    "security_id",
    "exchange_segment",
    "market_cap_cr",
    "isin",
    "sc_code",
    "series",
    "adv_20d",
]


def universe_dir() -> Path:
    d = state_dir() / "universe"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bhavcopy_cache_dir() -> Path:
    d = universe_dir() / "bhavcopy"
    d.mkdir(parents=True, exist_ok=True)
    return d


def universe_csv_path() -> Path:
    return universe_dir() / "universe.csv"


def compute_universe_frame(
    bhavs: pd.DataFrame,
    *,
    as_of: dt.date | None = None,
    equity_series: frozenset[str] = EQUITY_SERIES,
    adv_min: int = ADV_MIN_SHARES,
    adv_lookback: int = ADV_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Apply the backtest's universe filter and return a per-symbol frame.

    ``bhavs`` is the concatenated output of ``load_recent_bhavcopies`` — long
    panel of all BSE equity rows over the lookback window.

    ``as_of``: the anchor date (latest row). When None, uses the max date in
    ``bhavs``.
    """
    if bhavs.empty:
        return pd.DataFrame(columns=["sc_code", "isin", "symbol", "series", "adv_20d"])

    df = bhavs.copy()
    df = df[df["series"].isin(equity_series)]
    df = df.dropna(subset=["volume"])
    df = df[df["volume"] >= 0]
    # Key on sc_code (stable across renames); ISIN is joined later.
    df = df.sort_values(["sc_code", "date"]).reset_index(drop=True)

    anchor = pd.Timestamp(as_of) if as_of is not None else df["date"].max()
    if pd.isna(anchor):
        return pd.DataFrame(columns=["sc_code", "isin", "symbol", "series", "adv_20d"])

    # Rolling 20-day mean volume, per symbol. Take the anchor-day row.
    df["adv_20d"] = (
        df.groupby("sc_code")["volume"]
        .transform(lambda s: s.rolling(adv_lookback, min_periods=1).mean())
    )
    anchor_day = df[df["date"] == anchor].copy()
    if anchor_day.empty:
        # Fall back to the latest available date in the frame.
        latest = df["date"].max()
        anchor_day = df[df["date"] == latest].copy()
        log.warning("universe anchor %s missing; falling back to %s", anchor.date(), latest.date())

    anchor_day = anchor_day[anchor_day["adv_20d"] >= adv_min]
    anchor_day = anchor_day.drop_duplicates(subset=["sc_code"], keep="last")
    return anchor_day[["sc_code", "isin", "symbol", "series", "adv_20d"]].reset_index(drop=True)


def join_scrip_master(
    universe: pd.DataFrame,
    scrip: pd.DataFrame,
) -> pd.DataFrame:
    """Attach ``security_id`` + ``exchange_segment`` via sc_code join.

    For BSE equities, bhavcopy's ``FinInstrmId`` (= ``sc_code``) equals
    Dhan's ``SEM_SMST_SECURITY_ID``, so joining on sc_code gives us the
    ``security_id`` the order API needs.

    Rows in ``universe`` whose sc_code is absent from the scrip master are
    dropped — the live pipeline cannot place orders without a security_id.
    """
    if universe.empty:
        return pd.DataFrame(columns=UNIVERSE_CSV_COLUMNS)

    u = universe.copy()
    u["sc_code"] = u["sc_code"].astype(str).str.strip()
    u = u[u["sc_code"].str.len() > 0]

    scrip_j = scrip[["security_id", "exchange_segment", "trading_symbol"]].copy()
    scrip_j["sc_code"] = scrip_j["security_id"].astype(str).str.strip()
    # sc_code and security_id are identical for BSE equities. Dedupe the
    # scrip side so we don't explode the row count on join.
    scrip_j = scrip_j.drop_duplicates(subset=["sc_code"], keep="first")

    merged = u.merge(scrip_j, on="sc_code", how="inner")

    if merged.empty:
        return pd.DataFrame(columns=UNIVERSE_CSV_COLUMNS)

    # Prefer the bhavcopy ticker for `symbol` but fall back to the scrip
    # trading_symbol if the bhavcopy ticker is blank.
    merged["symbol"] = merged["symbol"].where(
        merged["symbol"].astype(str).str.len() > 0, merged["trading_symbol"]
    )
    merged["market_cap_cr"] = ""  # Champion B has mcap filter disabled; keep the column.

    out = merged[
        [
            "symbol",
            "security_id",
            "exchange_segment",
            "market_cap_cr",
            "isin",
            "sc_code",
            "series",
            "adv_20d",
        ]
    ].copy()
    out["adv_20d"] = out["adv_20d"].astype(float).round(0).astype("Int64")
    # Stable ordering so diffs across runs are minimal.
    return out.sort_values("symbol").reset_index(drop=True)


def write_universe_csv(df: pd.DataFrame, path: Path) -> None:
    """Write the universe to ``path`` atomically (temp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(UNIVERSE_CSV_COLUMNS)
        for _, r in df.iterrows():
            writer.writerow([r.get(c, "") for c in UNIVERSE_CSV_COLUMNS])
    tmp.replace(path)


def _stamp_settings(conn: sqlite3.Connection, count: int, source_date: str) -> None:
    with tx(conn):
        now_iso = now_ist().isoformat()
        for key, val in (
            (SETTINGS_KEY_UNIVERSE_REFRESH_AT, now_iso),
            (SETTINGS_KEY_UNIVERSE_COUNT, str(count)),
            (SETTINGS_KEY_UNIVERSE_SOURCE_DATE, source_date),
        ):
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, val, now_iso),
            )


def refresh_universe(
    *,
    end: dt.date | None = None,
    bhavcopy_lookback_days: int = 35,
    http_get_bhavcopy=None,
    http_get_scrip=None,
) -> Path:
    """Run the full refresh. Returns the path to the written CSV.

    ``end`` defaults to yesterday IST (today's bhavcopy only exists after
    ~16:30 IST; callers scheduling at 18:00 would typically pass ``now_ist
    ().date()``). The function tolerates missing days — it always uses the
    latest bhavcopy date actually available.

    ``http_get_*`` are injected for tests and can be omitted in production.
    """
    anchor = end or now_ist().date()
    log.info("universe refresh starting; anchor=%s lookback=%d", anchor, bhavcopy_lookback_days)

    # 1. Bhavcopy window.
    bhav_kwargs = {}
    if http_get_bhavcopy is not None:
        bhav_kwargs["http_get"] = http_get_bhavcopy
    bhavs = load_recent_bhavcopies(
        bhavcopy_cache_dir(),
        end=anchor,
        lookback_days=bhavcopy_lookback_days,
        **bhav_kwargs,
    )
    if bhavs.empty:
        raise RuntimeError(f"no bhavcopy data cached for window ending {anchor}")

    # 2. Scrip master.
    scrip_kwargs = {}
    if http_get_scrip is not None:
        scrip_kwargs["http_get"] = http_get_scrip
    try:
        scrip_path = fetch_scrip_master(universe_dir(), **scrip_kwargs)
    except ScripMasterError as e:
        raise RuntimeError(f"cannot refresh universe: {e}") from e
    scrip = load_bse_equities(scrip_path)

    # 3 + 4. Filter + ADV.
    uni = compute_universe_frame(bhavs, as_of=anchor)
    source_date = str(bhavs["date"].max().date())

    # 5. Join.
    joined = join_scrip_master(uni, scrip)

    # 6. Write.
    path = universe_csv_path()
    write_universe_csv(joined, path)
    log.info(
        "universe refresh complete: %d symbols (anchor=%s, source_date=%s) -> %s",
        len(joined), anchor, source_date, path,
    )

    # 7. Stamp settings.
    conn = connect()
    try:
        _stamp_settings(conn, count=len(joined), source_date=source_date)
    finally:
        conn.close()

    return path
