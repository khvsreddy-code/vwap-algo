def test_engine_has_separate_execution_worker():
    from engine import TradingEngine
    import pandas as pd

    class DummyClient:
        def history(self, *args, **kwargs):
            return pd.DataFrame()

    e = TradingEngine(
        DummyClient(), "NSE:NIFTY50-INDEX", "5", 15, 8, 65, False,
        {"underlying":"NIFTY","premium_min":170,"premium_max":210,"premium_target":190,"expiry_mode":"weekly","strikecount":5},
        {"enabled":False,"mode":"Points","sl_points":20,"target_points":40,"sl_percent":1,"target_percent":2,"sl_atr_mult":1,"target_atr_mult":2},
        session_start="09:15", session_end="15:15"
    )
    assert hasattr(e, "execution_queue")
    assert hasattr(e, "_execution_worker")
    assert e.entry_attempts == 0



def test_order_socket_state_is_initialized():
    from engine import TradingEngine
    import pandas as pd

    class DummyClient:
        def history(self, *args, **kwargs):
            return pd.DataFrame()

    e = TradingEngine(
        DummyClient(), "NSE:NIFTY50-INDEX", "5", 15, 8, 65, False,
        {"underlying":"NIFTY"},
        {"enabled":False},
        session_start="09:15", session_end="15:15"
    )
    assert e.order_socket is None
    assert e.order_ws_connected is False



def test_replay_confirmation_uses_high_low_and_emits_option_side():
    # Mirrors the historical replay rule directly: the crossing candle only
    # arms the setup; the next candle's high must reach cross-close + 15.
    import pandas as pd
    from strategy import VwapConfirmationEngine, StrategyConfig

    strat = VwapConfirmationEngine(StrategyConfig(15, 8))
    t0 = pd.Timestamp("2026-08-20 09:15", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-08-20 09:20", tz="Asia/Kolkata")

    cross = pd.Series({
        "datetime": t0,
        "date": t0.date(),
        "open": 100.0,
        "high": 111.0,
        "low": 99.0,
        "close": 110.0,
        "vwap": 105.0,
        "prev_close": 99.0,
        "prev_vwap": 101.0,
        "_algo_session_first": True,
    })
    trigger = pd.Series({
        "datetime": t1,
        "date": t1.date(),
        "open": 111.0,
        "high": 125.0,
        "low": 109.0,
        "close": 120.0,
        "vwap": 112.0,
        "prev_close": 110.0,
        "prev_vwap": 105.0,
        "_algo_session_first": False,
    })

    assert strat.process_closed_candle(cross) is None
    assert strat.confirmation_level == 125.0
    signal = strat.process_closed_candle(trigger)

    assert signal is not None
    assert signal["side"] == "BUY"
    assert signal["entry"] == 125.0
    assert signal["confirmation_level"] == 125.0
    assert signal["bars_since_cross"] == 1


def test_rounded_vwap_interaction_arms_and_confirms():
    # A small-body/doji candle that straddles VWAP should arm a setup even
    # without a textbook previous-close -> current-close cross. Confirmation
    # still requires the full 15 points on a later candle.
    import pandas as pd
    from strategy import VwapConfirmationEngine, StrategyConfig

    strat = VwapConfirmationEngine(StrategyConfig(15, 8))
    t0 = pd.Timestamp("2026-08-20 10:00", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-08-20 10:05", tz="Asia/Kolkata")

    rounded = pd.Series({
        "datetime": t0, "date": t0.date(),
        "open": 100.0, "high": 106.0, "low": 94.0, "close": 100.0,
        "vwap": 100.0, "prev_close": 98.5, "prev_vwap": 100.0,
        "_algo_session_first": False,
    })
    trigger = pd.Series({
        "datetime": t1, "date": t1.date(),
        "open": 101.0, "high": 116.0, "low": 99.0, "close": 112.0,
        "vwap": 101.0, "prev_close": 100.0, "prev_vwap": 100.0,
        "_algo_session_first": False,
    })

    strat.last_session_date = t0.date()
    assert strat.process_closed_candle(rounded) is None
    assert strat.cross_type == "VWAP_INTERACTION"
    assert strat.cross_direction == 1
    assert strat.confirmation_level == 115.0

    signal = strat.process_closed_candle(trigger)
    assert signal is not None
    assert signal["side"] == "BUY"
    assert signal["entry"] == 115.0
    assert signal["cross_type"] == "VWAP_INTERACTION"


def test_wick_touch_without_vwap_interaction_does_not_arm():
    import pandas as pd
    from strategy import VwapConfirmationEngine, StrategyConfig

    strat = VwapConfirmationEngine(StrategyConfig(15, 8))
    row = pd.Series({
        "datetime": pd.Timestamp("2026-08-20 10:00", tz="Asia/Kolkata"),
        "date": pd.Timestamp("2026-08-20", tz="Asia/Kolkata").date(),
        "open": 98.5, "high": 100.0, "low": 98.0, "close": 98.5,
        "vwap": 100.0, "prev_close": 99.0, "prev_vwap": 100.0,
        "_algo_session_first": False,
    })
    assert strat.process_closed_candle(row) is None
    assert strat.cross_bar is None


def test_vwap_bounce_arms_after_retest_and_confirms():
    import pandas as pd
    from strategy import VwapConfirmationEngine, StrategyConfig

    strat = VwapConfirmationEngine(StrategyConfig(15, 8))
    t0 = pd.Timestamp("2026-08-20 10:00", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-08-20 10:05", tz="Asia/Kolkata")

    prev = pd.Series({
        "datetime": t0 - pd.Timedelta(minutes=5), "date": t0.date(),
        "open": 120.0, "high": 123.0, "low": 119.0, "close": 121.0,
        "vwap": 110.0, "prev_close": 120.0, "prev_vwap": 109.0,
        "atr": 5.0, "_algo_session_first": False,
    })
    bounce = pd.Series({
        "datetime": t0, "date": t0.date(),
        "open": 109.5, "high": 112.5, "low": 109.2, "close": 111.0,
        "vwap": 110.0, "prev_close": 121.0, "prev_vwap": 110.0,
        "atr": 5.0, "_algo_session_first": False,
    })
    trigger = pd.Series({
        "datetime": t1, "date": t1.date(),
        "open": 112.0, "high": 126.0, "low": 110.0, "close": 124.0,
        "vwap": 111.0, "prev_close": 111.0, "prev_vwap": 110.0,
        "atr": 5.0, "_algo_session_first": False,
    })

    strat.last_session_date = prev["date"]
    strat.bar_index = 0
    # Prime the relationship state so the retest is classified as a bounce.
    strat._record_relationship(float(prev["close"]), float(prev["vwap"]), 0.5)

    assert strat.process_closed_candle(bounce) is None
    assert strat.cross_type in {"VWAP_BOUNCE", "VWAP_RECLAIM"}
    signal = strat.process_closed_candle(trigger)
    assert signal is not None
    assert signal["side"] == "BUY"
    assert signal["entry"] == 126.0


def test_failed_cross_reverses_direction_once():
    import pandas as pd
    from strategy import VwapConfirmationEngine, StrategyConfig

    strat = VwapConfirmationEngine(StrategyConfig(15, 8, allow_failed_cross=True))
    t0 = pd.Timestamp("2026-08-20 11:00", tz="Asia/Kolkata")
    t1 = pd.Timestamp("2026-08-20 11:05", tz="Asia/Kolkata")

    cross = pd.Series({
        "datetime": t0, "date": t0.date(),
        "open": 99.0, "high": 103.0, "low": 98.0, "close": 102.0,
        "vwap": 100.0, "prev_close": 98.0, "prev_vwap": 100.0,
        "atr": 3.0, "_algo_session_first": False,
    })
    failed = pd.Series({
        "datetime": t1, "date": t1.date(),
        "open": 101.0, "high": 102.0, "low": 96.0, "close": 97.0,
        "vwap": 100.0, "prev_close": 102.0, "prev_vwap": 100.0,
        "atr": 3.0, "_algo_session_first": False,
    })

    strat.last_session_date = t0.date()
    assert strat.process_closed_candle(cross) is None
    assert strat.cross_direction == 1
    assert strat.process_closed_candle(failed) is None
    assert strat.cross_direction == -1
    assert strat.cross_type == "FAILED_CROSS"
    assert strat.confirmation_level == 82.0


def test_live_chart_payload_rollover_logic_is_bounded_and_safe():
    from pathlib import Path
    source = Path(__file__).with_name("v9_4_53_live_chart.py").read_text()
    assert "df.tail(180)" in source
    assert 'max_candles=180' in source
    assert "can_delta = (" in source
    assert "previous[0] == signature[0]" in source
    assert 'core["mode"] = "full"' in source


def test_live_chart_does_not_send_old_markers_outside_visible_window():
    from pathlib import Path
    source = Path(__file__).with_name("v9_4_53_live_chart.py").read_text()
    assert 'item["time"] < visible_start' in source
    assert 'item["time"] > visible_end' in source
