import pandas as pd
from strategy import StrategyConfig, VwapConfirmationEngine

def row(close, vwap=100.0, open_=99.0, high=102.0, low=98.0, adx=28.0, slope=1.0, atr=4.0, atr_mean=3.5, dt="2026-01-02 09:20"):
    return pd.Series({"datetime": pd.Timestamp(dt), "date": pd.Timestamp(dt).date(), "open": open_, "high": high, "low": low, "close": close, "vwap": vwap, "prev_close": 99.0, "prev_vwap": 100.0, "adx": adx, "vwap_slope": slope, "atr": atr, "atr_mean": atr_mean})

def test_v10_defaults_preserve_confirmation_contract():
    cfg = StrategyConfig()
    assert cfg.confirmation_points == 15.0
    assert cfg.confirmation_bars == 8

def test_v10_quality_score_is_directional_and_available():
    e = VwapConfirmationEngine(StrategyConfig())
    score_bull, label_bull = e._quality_score(row(103), 1, "CLOSE_CROSS")
    score_bear, label_bear = e._quality_score(row(97, open_=101, high=102, low=96, slope=-1), -1, "CLOSE_CROSS")
    assert score_bull >= 4.0
    assert score_bear >= 4.0
    assert label_bull in {"HIGH", "GOOD", "WARMUP", "UNFILTERED"}
    assert label_bear in {"HIGH", "GOOD", "WARMUP", "UNFILTERED"}
