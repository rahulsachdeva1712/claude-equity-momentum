"""Daily universe construction: BSE bhavcopy -> filtered equity list joined
to Dhan scrip-master on ISIN so the live app can place orders on the same
set of symbols the Champion B backtest was finalized against.

Layout:
- ``bhavcopy.py``      — download + parse BSE bhavcopy CSVs (new + legacy).
- ``scrip_master.py``  — download + parse Dhan scrip master; ISIN lookup.
- ``refresh.py``       — orchestrator: run daily, produce ``universe.csv``
                          consumed by ``app.strategy.universe.CsvUniverseProvider``.

The universe definition matches ``research/backtest_2y/verify.py`` Champion B:
BSE main-board equity (``series in {A, B, T, X, XT}``) with 20-day average
daily volume >= 10_000 shares. No turnover floor. No market-cap filter.
"""
