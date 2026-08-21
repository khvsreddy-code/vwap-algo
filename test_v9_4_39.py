"""v9.4.39 regression tests: import smoke + false-trigger guard."""
import ast
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent


def test_python_sources_parse():
    for name in ("app.py", "engine.py", "strategy.py", "paper_trading.py", "live_chart.py", "fyers_client.py"):
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
