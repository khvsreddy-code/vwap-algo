import pandas as pd
from strategy import VwapConfirmationEngine, StrategyConfig

def run():
    cfg = StrategyConfig()
    assert cfg.confirmation_points == 15.0
    assert cfg.confirmation_bars == 8

    idx = pd.date_range("2026-08-21 09:15", periods=4, freq="5min", tz="Asia/Kolkata")
    rows = [
        (24230,24245,24220,24235,24240),
        (24235,24250,24230,24244,24243),  # cross close
        (24244,24250,24240,24248,24244),  # only +6
        (24248,24255,24240,24250,24245),  # only +11
    ]
    df = pd.DataFrame([
        dict(datetime=t, open=o, high=h, low=l, close=c, vwap=v)
        for t,(o,h,l,c,v) in zip(idx, rows)
    ])
    df["date"] = df["datetime"].dt.date
    df["prev_close"] = df["close"].shift(1)
    df["prev_vwap"] = df["vwap"].shift(1)

    strat = VwapConfirmationEngine(cfg)
    signals = []
    for _, row in df.iterrows():
        sig = strat.process_closed_candle(row)
        if sig:
            signals.append(sig)

    assert not signals, "False trigger: price never reached cross close + 15"

    # Add a candle that reaches 24259.
    extra = pd.Series({
        "datetime": pd.Timestamp("2026-08-21 09:35", tz="Asia/Kolkata"),
        "date": pd.Timestamp("2026-08-21").date(),
        "open": 24250, "high": 24260, "low": 24245, "close": 24259,
        "vwap": 24246, "prev_close": 24250, "prev_vwap": 24245,
    })
    sig = strat.process_closed_candle(extra)
    assert sig and sig["confirmation_level"] == 24259.0

if __name__ == "__main__":
    run()
    print("v9.4.38 regression test passed")
