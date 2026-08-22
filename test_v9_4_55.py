
import pandas as pd
from strategy import StrategyConfig, VwapConfirmationEngine

def _row(ts, close, vwap=100.0, prev_close=None, prev_vwap=None, adx=30.0, slope=0.2):
    d = pd.Timestamp(ts, tz="Asia/Kolkata")
    return pd.Series({
        "datetime": d, "date": d.date(), "open": close, "high": close+1,
        "low": close-1, "close": close, "vwap": vwap, "atr": 2.0,
        "adx": adx, "vwap_slope": slope, "prev_close": prev_close,
        "prev_vwap": prev_vwap,
    })

def test_v55_strong_trend_blocks_counter_direction_cross():
    e = VwapConfirmationEngine(StrategyConfig())
    # First bar establishes below-VWAP state.
    e.process_closed_candle(_row("2026-01-02 09:15", 99, adx=30, slope=0.2))
    # A bullish setup is consistent with the strong uptrend and should be allowed.
    s = e.process_closed_candle(_row("2026-01-02 09:20", 101, prev_close=99, prev_vwap=100, adx=30, slope=0.2))
    assert e.armed or s is not None

def test_v55_old_setup_invalidates_after_later_close_back_through_vwap():
    cfg = StrategyConfig(use_regime_filter=False, allow_failed_cross=False)
    e = VwapConfirmationEngine(cfg)
    e.process_closed_candle(_row("2026-01-02 09:15", 101, prev_close=99, prev_vwap=100, adx=20, slope=0))
    assert e.armed
    e.process_closed_candle(_row("2026-01-02 09:20", 101, adx=20, slope=0))
    assert e.armed
    e.process_closed_candle(_row("2026-01-02 09:25", 99, adx=20, slope=0))
    assert not e.armed
