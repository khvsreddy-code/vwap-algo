"""V10.1 targeted quality-layer regression tests."""
import pandas as pd
from strategy import VwapConfirmationEngine, StrategyConfig

def row(**kw):
    base = dict(datetime=pd.Timestamp("2026-08-21 10:00", tz="Asia/Kolkata"),
                date=pd.Timestamp("2026-08-21").date(), open=100.0, high=106.0,
                low=99.0, close=105.0, vwap=100.0, prev_close=99.0,
                prev_vwap=100.0, atr=10.0, adx=28.0, vwap_slope=0.5)
    base.update(kw)
    return pd.Series(base)

def test_no_daily_trade_quota():
    cfg = StrategyConfig()
    assert not hasattr(cfg, "max_entries_per_day")

def test_quality_warmup_never_blocks():
    s = VwapConfirmationEngine(StrategyConfig())
    assert s._quality_blocks(row(adx=None, atr=None, vwap_slope=None), 1, "CLOSE_CROSS") is False

def test_strong_countertrend_blocks_ordinary_long_cross():
    s = VwapConfirmationEngine(StrategyConfig())
    assert s._quality_blocks(row(close=105, vwap=100, vwap_slope=-1.0, adx=30.0), 1, "CLOSE_CROSS") is True

def test_reversal_setup_is_not_blocked_by_countertrend_rule():
    s = VwapConfirmationEngine(StrategyConfig())
    assert s._quality_blocks(row(close=105, vwap=100, vwap_slope=-1.0, adx=30.0), 1, "VWAP_RECLAIM") is False

def test_confirmation_contract_is_preserved():
    cfg = StrategyConfig()
    assert cfg.confirmation_points == 15.0
    assert cfg.confirmation_bars == 8

if __name__ == "__main__":
    test_no_daily_trade_quota()
    test_quality_warmup_never_blocks()
    test_strong_countertrend_blocks_ordinary_long_cross()
    test_reversal_setup_is_not_blocked_by_countertrend_rule()
    test_confirmation_contract_is_preserved()
    print("V10.1 tests passed")
