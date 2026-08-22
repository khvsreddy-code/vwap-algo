"""v9.4.53 regression tests for regime-aware VWAP entries and persistent live chart."""
import pandas as pd
from strategy import VwapConfirmationEngine, StrategyConfig

def test_regime_filter_does_not_impose_daily_trade_quota():
    cfg = StrategyConfig(15, 8)
    assert cfg.use_regime_filter is True
    # There is deliberately no max-trades-per-day setting.
    assert not hasattr(cfg, "max_entries_per_day")

def test_range_filter_blocks_only_clear_chop():
    cfg = StrategyConfig(15, 8)
    strat = VwapConfirmationEngine(cfg)
    strat._recent_relationships = [1, -1, 1, -1, 1, -1, 1, -1]
    row = pd.Series({
        "close": 100.0, "vwap": 100.0, "adx": 12.0,
        "atr": 10.0, "vwap_slope": 0.0,
    })
    assert strat.regime(row) == "RANGE"

def test_strong_direction_is_not_classified_as_range():
    cfg = StrategyConfig(15, 8)
    strat = VwapConfirmationEngine(cfg)
    strat._recent_relationships = [1, -1, 1, -1, 1, -1, 1, -1]
    row = pd.Series({
        "close": 120.0, "vwap": 100.0, "adx": 28.0,
        "atr": 10.0, "vwap_slope": 1.5,
    })
    assert strat.regime(row) == "TREND_BULL"

def test_two_independent_vwap_cycles_can_arm():
    cfg = StrategyConfig(15, 8)
    strat = VwapConfirmationEngine(cfg)
    t = pd.date_range("2026-08-21 09:15", periods=5, freq="5min", tz="Asia/Kolkata")
    rows = [
        (99, 100, 98, 99, 100),
        (99, 106, 99, 105, 100),  # bullish cross -> CE setup
        (105, 106, 104, 104, 100), # failed cross -> PE setup
        (104, 106, 103, 105, 100), # reclaim -> CE setup later
        (105, 121, 104, 120, 100), # confirmation of current CE setup
    ]
    signals=[]
    for i,(o,h,l,c,v) in enumerate(rows):
        r=pd.Series(dict(datetime=t[i],date=t[i].date(),open=o,high=h,low=l,close=c,vwap=v,
                         prev_close=rows[i-1][3] if i else None,
                         prev_vwap=rows[i-1][4] if i else None,
                         adx=30.0,vwap_slope=1.0,atr=10.0))
        sig=strat.process_closed_candle(r)
        if sig: signals.append(sig)
    assert len(signals) >= 1



def test_chart_module_uses_stable_component_key_and_incremental_payload():
    from pathlib import Path
    source = Path(__file__).with_name("v9_4_53_live_chart.py").read_text()
    assert 'componentKey' in source
    assert 'window.__fyersVwapCharts' in source
    assert 'host.parentElement !== root' in source
    assert 'mode": "delta"' in source

def test_engine_exposes_optional_order_socket_lifecycle():
    from engine import TradingEngine
    assert callable(getattr(TradingEngine, "start_order_socket", None))
    assert callable(getattr(TradingEngine, "stop_order_socket", None))


"""v9.4.53 tests for selective regime alignment and VWAP rejection setups."""
import pandas as pd
from strategy import VwapConfirmationEngine, StrategyConfig

def _row(close=105, open_=100, high=108, low=99, vwap=100, adx=30, slope=1.0):
    return pd.Series({
        "datetime": pd.Timestamp("2026-08-21 10:00", tz="Asia/Kolkata"),
        "date": pd.Timestamp("2026-08-21").date(),
        "open": open_, "high": high, "low": low, "close": close, "vwap": vwap,
        "prev_close": 103, "prev_vwap": 100, "adx": adx, "atr": 5.0,
        "vwap_slope": slope,
    })

def test_strong_bull_does_not_allow_countertrend_pe():
    strat = VwapConfirmationEngine(StrategyConfig())
    row = _row(close=105, adx=30, slope=1.0)
    assert strat._setup_allowed(row, -1, "CLOSE_CROSS") is False
    assert strat._setup_allowed(row, 1, "CLOSE_CROSS") is True

def test_range_blocks_plain_cross_but_allows_rejection():
    strat = VwapConfirmationEngine(StrategyConfig())
    row = _row(close=100.2, adx=12, slope=0.0)
    strat._recent_relationships = [1, -1, 1, -1, 1, -1, 1, -1]
    assert strat._setup_allowed(row, 1, "CLOSE_CROSS") is False
    assert strat._setup_allowed(row, 1, "VWAP_REJECTION") is True

def test_rounded_vwap_rejection_can_arm():
    cfg = StrategyConfig(confirmation_points=15, confirmation_bars=8)
    strat = VwapConfirmationEngine(cfg)
    strat._recent_relationships = [1, 1, 1, 1, 1, 1, 1, 1]
    row = _row(close=104, open_=102, high=105, low=99.5, vwap=100, adx=28, slope=0.8)
    detected = strat._detect_vwap_cross(row, first_session_candle=False)
    assert detected is not None
    assert detected[0] == 1
    assert detected[1] in {"VWAP_REJECTION", "VWAP_BOUNCE", "VWAP_RECLAIM"}

def test_no_daily_trade_quota_added():
    cfg = StrategyConfig()
    assert not hasattr(cfg, "max_entries_per_day")
