"""Strategy parameters. Mirrors FRD A.4 exactly."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    universe: str = "all_bse_equities"

    rsi_period: int = 14
    mfi_period: int = 14
    cci_period: int = 14
    atr_period: int = 20
    ema_fast_period: int = 21
    ema_slow_period: int = 50

    use_rsi_filter: bool = True
    use_mfi_filter: bool = False
    use_cci_filter: bool = False
    use_atr_filter: bool = True

    rsi_threshold: float = 75.0
    mfi_threshold: float = 70.0
    cci_threshold: float = 110.0
    atr_pct_max: float = 0.04

    sort_metric: str = "relative_return_126d"
    weight_metric: str = "relative_return_252d"

    max_positions: int = 5
    min_positions: int = 1
    full_rebalance: bool = True

    market_cap_min_cr: float = 100.0
    breadth_threshold: float = 0.0

    explicit_txn_cost_bps: float = 10.0  # 10 bps; FRD A.4

    lookback_63d: int = 63
    lookback_126d: int = 126
    lookback_252d: int = 252


DEFAULT_CONFIG = StrategyConfig()
