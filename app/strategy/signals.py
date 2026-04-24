"""Signal engine. Implements FRD A.5-A.7 end to end.

Input: per-symbol OHLCV panel (long-form DataFrame with symbol, date, OHLCV,
market_cap_cr). Output: a TargetSet for a given signal_date with selected
symbols, target weights, and a priced target quantity given available capital.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
import numpy as np
import pandas as pd

from app.strategy.config import DEFAULT_CONFIG, StrategyConfig
from app.strategy import indicators as ind


@dataclass(frozen=True)
class TargetRow:
    symbol: str
    security_id: str | None
    exchange_segment: str | None
    selected: bool
    rank_by_126d: int | None
    weight: float
    reference_price: float
    target_value: float
    target_qty: int
    rel_126d: float
    rel_252d: float


@dataclass(frozen=True)
class TargetSet:
    session_date: date
    rows: tuple[TargetRow, ...]
    capital: float

    def selected(self) -> tuple[TargetRow, ...]:
        return tuple(r for r in self.rows if r.selected)


def _compute_per_symbol(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    df = df.sort_values("date").copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"] if "volume" in df.columns else pd.Series(0.0, index=df.index)

    df["ema_fast"] = ind.ema(close, cfg.ema_fast_period)
    df["ema_slow"] = ind.ema(close, cfg.ema_slow_period)
    df["rsi"] = ind.rsi(close, cfg.rsi_period)
    df["atr_pct"] = ind.atr_pct(high, low, close, cfg.atr_period)

    if cfg.use_mfi_filter:
        df["mfi"] = ind.mfi(high, low, close, volume, cfg.mfi_period)
    if cfg.use_cci_filter:
        df["cci"] = ind.cci(high, low, close, cfg.cci_period)

    df["return_63d"] = ind.n_day_return(close, cfg.lookback_63d)
    df["return_126d"] = ind.n_day_return(close, cfg.lookback_126d)
    df["return_252d"] = ind.n_day_return(close, cfg.lookback_252d)

    return df


def compute_universe_metrics(panel: pd.DataFrame, cfg: StrategyConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """Compute indicators + relative returns on a long-form OHLCV panel.

    Required columns: symbol, date, open, high, low, close.
    Optional: volume, security_id, exchange_segment, market_cap_cr. The
    market-cap column is only consulted when ``cfg.use_mcap_filter`` is
    True — Champion B runs with the gate off (see config comment).
    """
    required = {"symbol", "date", "open", "high", "low", "close"}
    if cfg.use_mcap_filter:
        required = required | {"market_cap_cr"}
    missing = required - set(panel.columns)
    if missing:
        raise ValueError(f"panel missing columns: {sorted(missing)}")

    parts = [_compute_per_symbol(g, cfg) for _, g in panel.groupby("symbol", sort=False)]
    per_sym = pd.concat(parts, ignore_index=True) if parts else panel.copy()
    # Relative returns: symbol return minus same-day universe mean.
    for n in ("63d", "126d", "252d"):
        col = f"return_{n}"
        universe_mean = per_sym.groupby("date")[col].transform("mean")
        per_sym[f"relative_return_{n}"] = per_sym[col] - universe_mean
    return per_sym


def _static_eligible_mask(row: pd.Series, cfg: StrategyConfig) -> bool:
    """All eligibility checks that depend only on daily indicators. FRD A.5
    minus the intraday volume gate."""
    if cfg.use_mcap_filter:
        if not np.isfinite(row.get("market_cap_cr", np.nan)):
            return False
        if row["market_cap_cr"] < cfg.market_cap_min_cr:
            return False
    for col in ("ema_fast", "ema_slow", "close"):
        if not np.isfinite(row.get(col, np.nan)):
            return False
    if not (row["close"] > row["ema_fast"]):
        return False
    if not (row["ema_fast"] > row["ema_slow"]):
        return False
    if cfg.use_rsi_filter:
        if not np.isfinite(row.get("rsi", np.nan)) or row["rsi"] < cfg.rsi_threshold:
            return False
    if cfg.use_atr_filter:
        if not np.isfinite(row.get("atr_pct", np.nan)) or row["atr_pct"] > cfg.atr_pct_max:
            return False
    if not np.isfinite(row.get(cfg.sort_metric, np.nan)):
        return False
    if not np.isfinite(row.get(cfg.weight_metric, np.nan)):
        return False
    return True


def _volume_ok(symbol: str, intraday_volumes: dict[str, float] | None, cfg: StrategyConfig) -> bool:
    """FRD A.5 intraday liquidity gate: `vol_0925_0930 >= cfg.intraday_volume_min`.

    Fails closed: if the filter is enabled and no volume is provided for a
    symbol, the symbol is ineligible (matches A.2 — missing 09:25–09:29
    candles disqualify the symbol for that session).
    """
    if not cfg.use_volume_filter:
        return True
    if intraday_volumes is None:
        return False
    v = intraday_volumes.get(symbol)
    if v is None or not np.isfinite(v):
        return False
    return float(v) >= float(cfg.intraday_volume_min)


def static_eligible_symbols(
    metrics: pd.DataFrame,
    session_date: date,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    reference_date: date | None = None,
) -> list[str]:
    """Symbols that pass the static (non-volume) eligibility filters.

    `session_date` is the date the rebalance is stored under. `reference_date`
    is the completed-day EoD bar that the ranking/filtering reads from; it
    defaults to `session_date` (the backtest case) but in live trading at
    09:30 IST today's EoD bar hasn't been published yet, so the caller passes
    the latest bar present in `metrics` (typically the prior trading day).
    """
    ref = reference_date if reference_date is not None else session_date
    day = metrics[metrics["date"] == pd.Timestamp(ref)]
    if day.empty:
        return []
    mask = day.apply(lambda r: _static_eligible_mask(r, cfg), axis=1)
    return [str(s) for s in day.loc[mask, "symbol"].tolist()]


def _target_weights(selected: pd.DataFrame, cfg: StrategyConfig) -> list[float]:
    """Compute per-position target weights under the configured weight scheme.

    FRD A.7. Supported schemes:
      - "inv_atr":  w_i proportional to 1 / atr_pct_i (lower vol -> larger weight)
      - "rel":      w_i proportional to `weight_metric` (legacy: relative_return_252d)
      - "rel_rank": w_i proportional to `sort_metric`
      - "equal":    w_i = 1/N

    Any non-positive contributor is clipped to zero before normalization; if
    the resulting sum is non-positive, we fall back to equal weights.
    """
    n = len(selected)
    if n == 0:
        return []
    scheme = cfg.weight_scheme
    if scheme == "equal":
        return [1.0 / n] * n
    if scheme == "inv_atr":
        a = selected["atr_pct"].clip(lower=0.001).to_numpy(dtype=float)
        raw = 1.0 / a
    elif scheme == "rel_rank":
        raw = selected[cfg.sort_metric].clip(lower=0.0).to_numpy(dtype=float)
    elif scheme == "rel":
        raw = selected[cfg.weight_metric].clip(lower=0.0).to_numpy(dtype=float)
    else:
        raise ValueError(f"unknown weight_scheme: {scheme!r}")
    s = float(raw.sum())
    if s <= 0:
        return [1.0 / n] * n
    return (raw / s).tolist()


def build_target_set(
    metrics: pd.DataFrame,
    session_date: date,
    capital: float,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    intraday_volumes: dict[str, float] | None = None,
    reference_date: date | None = None,
) -> TargetSet:
    """Run selection + sizing for a given session_date. FRD A.6, A.7, A.10.

    `metrics` is the output of compute_universe_metrics. `capital` is the cash
    available to deploy on this rebalance. `session_date` is the date the
    signals are stored under. `reference_date` is the completed-day EoD bar
    the ranking reads from; defaults to session_date (backtest case). In live
    trading at 09:30 IST, today's EoD bar doesn't exist yet, so the caller
    passes the latest bar present in metrics (typically prior trading day).

    `intraday_volumes` is a dict mapping symbol -> `vol_0925_0930` (summed
    traded volume across the five one-minute candles ending at 09:30 IST on
    `session_date`). Required when `cfg.use_volume_filter` is True — a symbol
    absent from this dict is treated as failing the liquidity gate. Per A.10,
    the volume filter is part of eligibility, so the top-5 is ranked from the
    volume-qualified set only.
    """
    ref = reference_date if reference_date is not None else session_date
    day = metrics[metrics["date"] == pd.Timestamp(ref)]
    if day.empty:
        return TargetSet(session_date=session_date, rows=(), capital=capital)

    def _eligible(row: pd.Series) -> bool:
        return _static_eligible_mask(row, cfg) and _volume_ok(str(row["symbol"]), intraday_volumes, cfg)

    eligible = day[day.apply(_eligible, axis=1)].copy()
    if eligible.empty:
        return TargetSet(session_date=session_date, rows=(), capital=capital)

    eligible = eligible.sort_values(cfg.sort_metric, ascending=False)
    selected = eligible.head(cfg.max_positions).copy()

    if len(selected) < cfg.min_positions:
        return TargetSet(session_date=session_date, rows=(), capital=capital)

    weights = _target_weights(selected, cfg)
    selected["weight"] = weights
    selected["target_value"] = selected["weight"] * capital

    cost_factor = cfg.explicit_txn_cost_bps / 10_000.0

    rows: list[TargetRow] = []
    for rank, (_, r) in enumerate(selected.iterrows(), start=1):
        ref_price = float(r["close"])
        target_value_after_cost = float(r["target_value"]) * (1.0 - cost_factor)
        target_qty = int(math.floor(target_value_after_cost / ref_price)) if ref_price > 0 else 0
        rows.append(
            TargetRow(
                symbol=str(r["symbol"]),
                security_id=str(r.get("security_id")) if "security_id" in r else None,
                exchange_segment=str(r.get("exchange_segment")) if "exchange_segment" in r else None,
                selected=True,
                rank_by_126d=rank,
                weight=float(r["weight"]),
                reference_price=ref_price,
                target_value=float(r["target_value"]),
                target_qty=target_qty,
                rel_126d=float(r.get("relative_return_126d", np.nan)),
                rel_252d=float(r.get("relative_return_252d", np.nan)),
            )
        )
    return TargetSet(session_date=session_date, rows=tuple(rows), capital=capital)
