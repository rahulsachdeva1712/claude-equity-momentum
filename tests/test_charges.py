import pytest

from app.charges import (
    BROKERAGE_CNC,
    EXCH_TXN_BSE,
    GST_RATE,
    SEBI_TURNOVER,
    STAMP_DUTY_BUY,
    STT_DELIVERY_SELL,
    Side,
    compute_charges,
    non_broker_charges,
)


def test_buy_has_stamp_no_stt():
    b = compute_charges(Side.BUY, qty=100, price=1000.0)
    assert b.stt == 0.0
    assert b.stamp_duty == pytest.approx(100 * 1000 * STAMP_DUTY_BUY)
    assert b.brokerage == 0.0


def test_sell_has_stt_no_stamp():
    b = compute_charges(Side.SELL, qty=100, price=1000.0)
    assert b.stamp_duty == 0.0
    assert b.stt == pytest.approx(100 * 1000 * STT_DELIVERY_SELL)


def test_total_equals_sum_of_parts():
    b = compute_charges(Side.BUY, qty=10, price=500.0)
    parts = b.brokerage + b.stt + b.exchange_txn + b.sebi_turnover + b.stamp_duty + b.gst
    assert b.total == pytest.approx(parts, rel=1e-6)


def test_gst_applied_on_brokerage_exch_sebi_only():
    notional = 10_000
    b = compute_charges(Side.BUY, qty=10, price=1000.0)
    expected_gst = (BROKERAGE_CNC + EXCH_TXN_BSE + SEBI_TURNOVER) * notional * GST_RATE
    assert b.gst == pytest.approx(expected_gst, rel=1e-6)


def test_non_broker_charges_excludes_brokerage():
    b = compute_charges(Side.SELL, qty=50, price=200.0)
    assert non_broker_charges(b) == pytest.approx(b.total - b.brokerage, rel=1e-6)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        compute_charges(Side.BUY, qty=0, price=100.0)
    with pytest.raises(ValueError):
        compute_charges(Side.BUY, qty=1, price=-1.0)
