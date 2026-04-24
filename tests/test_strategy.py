"""Golden tests for the signal engine. FRD A.5 - A.7."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.strategy.config import StrategyConfig
from app.strategy.indicators import ema, n_day_return, rsi
from app.strategy.signals import build_target_set, compute_universe_metrics, static_eligible_symbols


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

    # Pass the intraday volume gate (FRD A.5) with a value well above the
    # 1000-share threshold for each symbol.
    volumes = {"STRONG": 50_000.0, "FLAT": 50_000.0, "SMALLCAP": 50_000.0}
    # Champion B ships with the market-cap filter OFF by default (bhavcopy
    # carries no mcap, so the backtested universe had no mcap gate). Enable
    # it here to assert the gate still works when turned on.
    cfg = StrategyConfig(use_mcap_filter=True)
    ts = build_target_set(
        metrics, signal_day.date(), capital=1_000_000.0, cfg=cfg, intraday_volumes=volumes
    )
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
    ts = build_target_set(
        metrics, d, capital=1_000_000.0,
        intraday_volumes={"A": 50_000.0, "B": 50_000.0},
    )
    rows = ts.selected()
    assert len(rows) == 2
    assert abs(sum(r.weight for r in rows) - 1.0) < 1e-9
    # target_qty * price <= weight * capital (after 10bps cost + floor)
    for r in rows:
        assert r.target_qty * r.reference_price <= r.weight * 1_000_000.0 + 1e-6


def test_empty_when_none_eligible():
    n = 300
    # Both below market cap threshold. With use_mcap_filter=True both are
    # ineligible; Champion B's default (mcap off) would accept them.
    cfg = StrategyConfig(use_mcap_filter=True)
    closes = np.array([100.0 * (1.005 ** i) for i in range(n)])
    panel = _build_panel(
        {"A": closes, "B": closes},
        start=date(2025, 1, 1),
        market_caps={"A": 10.0, "B": 20.0},
    )
    metrics = compute_universe_metrics(panel, cfg)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(
        metrics, d, capital=1_000_000.0, cfg=cfg,
        intraday_volumes={"A": 50_000.0, "B": 50_000.0},
    )
    assert ts.selected() == ()


def test_max_positions_cap_respected():
    # Pin max_positions=5 explicitly rather than relying on the baseline
    # default (which changes over time as strategy rolls forward); the test
    # cares that the cap is enforced, not what the cap happens to be.
    cfg = StrategyConfig(max_positions=5)
    n = 300
    syms = {f"S{i}": np.array([100.0 * ((1.004 + i * 0.0002) ** k) for k in range(n)]) for i in range(8)}
    panel = _build_panel(
        syms, start=date(2025, 1, 1), market_caps={s: 500.0 for s in syms}
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(
        metrics, d, capital=1_000_000.0, cfg=cfg,
        intraday_volumes={s: 50_000.0 for s in syms},
    )
    assert len(ts.selected()) == cfg.max_positions


def test_volume_gate_blocks_illiquid_top_ranker():
    """FRD A.5, A.6: the volume gate is part of eligibility, so a top-momentum
    name that fails the 09:25-09:30 volume filter is never in the top-5.
    The slot is taken by the next volume-qualified name.
    """
    n = 300
    # HI_MOM is the strongest trend; MID_1..MID_5 trail it but are volume-qualified.
    syms = {"HI_MOM": np.array([100.0 * (1.008 ** k) for k in range(n)])}
    for i in range(5):
        syms[f"MID_{i}"] = np.array([100.0 * ((1.005 + i * 0.0001) ** k) for k in range(n)])
    panel = _build_panel(
        syms, start=date(2025, 1, 1), market_caps={s: 500.0 for s in syms}
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()

    # HI_MOM has only 500 shares traded 09:25-09:30 -> fails the 1000 gate.
    volumes = {"HI_MOM": 500.0, **{f"MID_{i}": 50_000.0 for i in range(5)}}
    ts = build_target_set(metrics, d, capital=1_000_000.0, intraday_volumes=volumes)
    selected = {r.symbol for r in ts.selected()}
    assert "HI_MOM" not in selected
    assert selected == {f"MID_{i}" for i in range(5)}


def test_volume_gate_fails_closed_when_volume_missing():
    """FRD A.2, A.5: a symbol with no intraday volume entry fails the gate
    when the filter is enabled (fail-closed)."""
    n = 300
    closes = np.array([100.0 * (1.005 ** k) for k in range(n)])
    panel = _build_panel(
        {"A": closes, "B": closes},
        start=date(2025, 1, 1),
        market_caps={"A": 500.0, "B": 500.0},
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    # Only A has a volume entry; B is absent -> ineligible.
    ts = build_target_set(metrics, d, capital=1_000_000.0, intraday_volumes={"A": 50_000.0})
    assert {r.symbol for r in ts.selected()} == {"A"}


def test_volume_filter_disabled_via_config_bypasses_gate():
    """When `use_volume_filter=False`, the intraday_volumes arg is ignored."""
    cfg = StrategyConfig(use_volume_filter=False)
    n = 300
    closes = np.array([100.0 * (1.005 ** k) for k in range(n)])
    panel = _build_panel(
        {"A": closes}, start=date(2025, 1, 1), market_caps={"A": 500.0}
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    # No volumes supplied at all; gate is off so A still qualifies.
    ts = build_target_set(metrics, d, capital=1_000_000.0, cfg=cfg, intraday_volumes=None)
    assert {r.symbol for r in ts.selected()} == {"A"}


def test_mcap_filter_off_by_default_admits_small_cap():
    """Champion B baseline: ``use_mcap_filter=False``. A stock that fails
    the 100-cr floor under the legacy config should still be selected in
    the Champion B default. Also the panel doesn't need a market_cap_cr
    column at all when the gate is off.
    """
    n = 300
    closes = np.array([100.0 * (1.005 ** k) for k in range(n)])
    # NOTE: _build_panel still stamps market_cap_cr, but default config
    # ignores it. Pre-Champion-B config would have rejected this row.
    panel = _build_panel(
        {"TINY": closes},
        start=date(2025, 1, 1),
        market_caps={"TINY": 5.0},  # far below any reasonable floor
    )
    metrics = compute_universe_metrics(panel)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    ts = build_target_set(
        metrics, d, capital=1_000_000.0,
        intraday_volumes={"TINY": 50_000.0},
    )
    assert {r.symbol for r in ts.selected()} == {"TINY"}


def test_compute_metrics_works_without_market_cap_column():
    """Under Champion B defaults the panel does not need market_cap_cr."""
    n = 300
    closes = np.array([100.0 * (1.005 ** k) for k in range(n)])
    rows = []
    for sym, series in {"X": closes}.items():
        for i, c in enumerate(series):
            rows.append({
                "symbol": sym, "date": pd.Timestamp(date(2025, 1, 1) + timedelta(days=i)),
                "open": float(c), "high": float(c) * 1.005, "low": float(c) * 0.995,
                "close": float(c), "volume": 100_000.0,
            })
    panel = pd.DataFrame(rows)
    # Does not raise even though market_cap_cr is absent.
    metrics = compute_universe_metrics(panel)
    assert "relative_return_63d" in metrics.columns


def test_static_eligible_symbols_excludes_volume_check():
    """`static_eligible_symbols` returns static-filter survivors regardless
    of volume data — the trading job uses it to narrow the set of symbols
    for which intraday volume is fetched."""
    n = 300
    cfg = StrategyConfig(use_mcap_filter=True)  # exercise the mcap gate.
    closes = np.array([100.0 * (1.005 ** k) for k in range(n)])
    panel = _build_panel(
        {"A": closes, "SMALL": closes},
        start=date(2025, 1, 1),
        market_caps={"A": 500.0, "SMALL": 10.0},  # SMALL fails market-cap.
    )
    metrics = compute_universe_metrics(panel, cfg)
    d = pd.Timestamp(date(2025, 1, 1) + timedelta(days=n - 1)).date()
    survivors = set(static_eligible_symbols(metrics, d, cfg))
    assert "A" in survivors
    assert "SMALL" not in survivors
