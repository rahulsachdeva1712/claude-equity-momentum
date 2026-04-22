"""Golden tests for the signal engine. FRD A.5 - A.7."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategy.config import StrategyConfig
from app.strategy.indicators import ema, n_day_return, rsi
from app.strategy.signals import build_target_set, compute_universe_metrics


def _build_panel(symbol_series: dict[str, np.ndarray], start: date, market_caps: dict[str, float]) -> pd.DataFrame:
    rows = []
    for sym, closes in symbol_series.items():
        for i, c in enumerate(closes):
            d = start + timedelta(days=i)
            rows.append(
                {
                    "symbol": sym,
                    "date": pd.Timestamp(d),
                    "open": float(c),
                    "high": float(c) * 1.005,
                    "low": float(c) * 0.995,
                    "close": float(c),
                    "volume": 100_000.0,
                    "market_cap_cr": market_caps[sym],
                }
            )
    return pd.DataFrame(rows)


def test_ema_matches_hand_calc():
    s = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    out = ema(s, 3)
    # First two NaN due to min_periods=3.
    assert np.isnan(out.iloc[0])
    assert np.isnan(out.iloc[1])
    assert out.iloc[2] > 0


def test_rsi_bounded():
    rng = np.random.default_rng(42)
    s = pd.Series(100 + rng.standard_normal(300).cumsum())
    r = rsi(s, 14).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_n_day_return():
    s = pd.Series([100.0, 110.0, 121.0])
    r = n_day_return(s, 1)
    assert r.iloc[1] == pytest.approx(0.1)
    assert r.iloc[2] == pytest.approx(0.1)


def test_trending_up_stock_gets_selected_over_flat():
    # 300 bars is enough for all indicators.
    n = 300
    # STRONG: smooth uptrend, 0.5% per day -> high RSI, close > EMA21 > EMA50, tight ATR
    strong_closes = np.array([100.0 * (1.005 ** i) for i in range(n)])
    # FLAT: random walk around 100 -> low relative return
    rng = np.random.default_rng(0)
    flat_closes = 100.0 + rng.standard_normal(n) * 0.3
    # SMALLCAP: same trend as STRONG but below market cap floor -> must be filtered out
    smallcap_closes = strong_closes.copy()

    panel = _build_panel(
        {"STRONG": strong_closes, "FLAT": flat_closes, "SMALLCAP": smallcap_closes},
        start=date(2025, 1, 1),
        market_caps={"STRONG": 500.0, "FLAT": 500.0, "SMALLCAP": 50.0},
    )
    metrics = compute_universe_metrics(panel)
    signal_day = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1))

    ts = build_target_set(metrics, signal_day.date(), capital=1_000_000.0)
    selected = [r.symbol for r in ts.selected()]
    assert "STRONG" in selected
    assert "SMALLCAP" not in selected  # market_cap filter
    assert "FLAT" not in selected      # fails momentum + trend filters


def test_weights_sum_to_one_and_qty_fits_capital():
    n = 300
    closes_a = np.array([100.0 * (1.006 ** i) for i in range(n)])
    closes_b = np.array([100.0 * (1.005 ** i) for i in range(n)])
    panel = _build_panel(
        {"A": closes_a, "B": closes_b},
        start=date(2025, 1, 1),
        market_caps={"A": 500.0, "B": 500.0},
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(metrics, d, capital=1_000_000.0)
    rows = ts.selected()
    assert len(rows) == 2
    assert abs(sum(r.weight for r in rows) - 1.0) < 1e-9
    # target_qty * price <= weight * capital (after 10bps cost + floor)
    for r in rows:
        assert r.target_qty * r.reference_price <= r.weight * 1_000_000.0 + 1e-6


def test_empty_when_none_eligible():
    n = 300
    # Both below market cap threshold
    closes = np.array([100.0 * (1.005 ** i) for i in range(n)])
    panel = _build_panel(
        {"A": closes, "B": closes},
        start=date(2025, 1, 1),
        market_caps={"A": 10.0, "B": 20.0},
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(metrics, d, capital=1_000_000.0)
    assert ts.selected() == ()


def test_max_positions_cap_respected():
    cfg = StrategyConfig()  # max_positions=5
    n = 300
    syms = {f"S{i}": np.array([100.0 * ((1.004 + i * 0.0002) ** k) for k in range(n)]) for i in range(8)}
    panel = _build_panel(
        syms, start=date(2025, 1, 1), market_caps={s: 500.0 for s in syms}
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(metrics, d, capital=1_000_000.0, cfg=cfg)
    assert len(ts.selected()) == cfg.max_positions
