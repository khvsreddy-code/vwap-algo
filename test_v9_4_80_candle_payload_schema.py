from app import _candle_payload

row = {
    "symbol": "NSE:NIFTY50-INDEX",
    "underlying": "NSE:NIFTY50-INDEX",
    "active": True,
    "expiry": None,
    "strike": None,
    "option_type": None,
    "candle_start": "2026-08-27T09:15:00+05:30",
    "open": 1, "high": 2, "low": 0.5, "close": 1.5,
    "ltp": 1.5, "volume": 10, "oi": None, "oi_change": None,
    "prev_oi": None, "oi_snapshot_at": None,
    "source": "fyers_history_eod_recovery",
}
clean = _candle_payload(row)
assert "active" not in clean
assert clean["symbol"] == row["symbol"]
assert clean["candle_start"] == row["candle_start"]
print("v9.4.80 candle payload schema check passed")
