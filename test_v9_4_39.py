"""v9.4.39 regression tests: import smoke + false-trigger guard."""
import ast
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent


def test_python_sources_parse():
    for name in ("app.py", "engine.py", "strategy.py", "paper_trading.py", "v9_4_49_live_chart.py", "fyers_client.py"):
        ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)


def test_core_imports():
    # These are the modules named by the Streamlit traceback. Importing them
    # here catches broken/circular module state before the app is deployed.
    import engine  # noqa: F401
    import paper_trading  # noqa: F401
    import strategy  # noqa: F401


def test_false_trigger_guard():
    from strategy import VwapConfirmationEngine, StrategyConfig

    cfg = StrategyConfig(confirmation_points=15.0, confirmation_bars=8)
    idx = pd.date_range("2026-08-21 09:15", periods=4, freq="5min", tz="Asia/Kolkata")
    rows = [
        (24230, 24245, 24220, 24235, 24240),
        (24235, 24250, 24230, 24244, 24243),  # closed above VWAP; arms BUY
        (24244, 24250, 24240, 24248, 24244),  # +6 only: must not trigger
        (24248, 24255, 24240, 24250, 24245),  # +11 only: must not trigger
    ]
    df = pd.DataFrame([
        dict(datetime=t, open=o, high=h, low=l, close=c, vwap=v)
        for t, (o, h, l, c, v) in zip(idx, rows)
    ])
    df["date"] = df["datetime"].dt.date
    df["prev_close"] = df["close"].shift(1)
    df["prev_vwap"] = df["vwap"].shift(1)

    strat = VwapConfirmationEngine(cfg)
    assert all(strat.process_closed_candle(row) is None for _, row in df.iterrows())
    assert strat.cross_price == 24244.0
    assert strat.confirmation_level == 24259.0


if __name__ == "__main__":
    test_python_sources_parse()
    test_core_imports()
    test_false_trigger_guard()
    print("v9.4.39 regression tests passed")

def test_history_seed_preserves_pending_setup_for_first_live_candle():
    from strategy import VwapConfirmationEngine, StrategyConfig

    cfg = StrategyConfig(confirmation_points=10.0, confirmation_bars=8)
    idx = pd.date_range("2026-08-21 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
    closed = pd.DataFrame([
        {"datetime": idx[0], "open": 99, "high": 101, "low": 98, "close": 99, "volume": 1},
        {"datetime": idx[1], "open": 99, "high": 106, "low": 99, "close": 105, "volume": 1},
    ])
    prepared = VwapConfirmationEngine.prepare(closed)

    strat = VwapConfirmationEngine(cfg)
    strat.seed_from_history(prepared)

    assert strat.cross_price == 105.0
    assert strat.confirmation_level == 115.0
    assert strat.last_session_date == idx[1].date()

    live = VwapConfirmationEngine.prepare(pd.concat([
        closed,
        pd.DataFrame([{
            "datetime": idx[2], "open": 105, "high": 116,
            "low": 104, "close": 115, "volume": 1
        }])
    ], ignore_index=True)).iloc[-1]

    signal = strat.process_closed_candle(live)
    assert signal is not None
    assert signal["entry"] == 115.0



def test_live_tick_reaches_confirmation_without_waiting_for_candle_close():
    from strategy import VwapConfirmationEngine, StrategyConfig

    cfg = StrategyConfig(confirmation_points=15.0, confirmation_bars=8)
    idx = pd.date_range("2026-08-21 09:15", periods=2, freq="5min", tz="Asia/Kolkata")
    df = pd.DataFrame([
        {"datetime": idx[0], "open": 24230, "high": 24240, "low": 24220, "close": 24235, "volume": 1},
        {"datetime": idx[1], "open": 24235, "high": 24250, "low": 24230, "close": 24244, "volume": 1},
    ])
    prepared = VwapConfirmationEngine.prepare(df)
    # Make the first candle clearly below VWAP so the second candle is the
    # actual closed cross used by this regression test.
    prepared["vwap"] = [24240.0, 24243.0]
    prepared["prev_vwap"] = prepared["vwap"].shift(1)

    strat = VwapConfirmationEngine(cfg)
    strat.seed_from_history(prepared)

    # The last closed candle armed BUY at 24244 -> 24259. The next candle
    # should trigger the instant a live tick reaches 24259, even before close.
    signal = strat.process_live_tick(
        24259.0,
        timestamp=pd.Timestamp("2026-08-21 09:25", tz="Asia/Kolkata"),
    )
    assert signal is not None
    assert signal["side"] == "BUY"
    assert signal["entry"] == 24259.0
