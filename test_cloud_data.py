from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from cloud_data import CloudCandleRecorder

IST = ZoneInfo('Asia/Kolkata')


class FakeStore:
    def __init__(self):
        self.instruments = []
        self.writes = []

    def upsert_instruments(self, rows):
        self.instruments.extend(rows)

    def upsert_candles(self, rows):
        self.writes.extend(rows)
        return len(rows)


def test_recorder_writes_nifty_and_options_separately():
    store = FakeStore()
    meta = {
        'NSE:NIFTY50-INDEX': {'symbol':'NSE:NIFTY50-INDEX','underlying':'NIFTY','expiry':None,'strike':None,'option_type':None},
        'NSE:NIFTYTESTCE': {'symbol':'NSE:NIFTYTESTCE','underlying':'NIFTY','expiry':'2026-08-27','strike':25000,'option_type':'CE'},
        'NSE:NIFTYTESTPE': {'symbol':'NSE:NIFTYTESTPE','underlying':'NIFTY','expiry':'2026-08-27','strike':25000,'option_type':'PE'},
    }
    rec = CloudCandleRecorder(store, meta)
    rec.start()
    base = int(pd.Timestamp('2026-08-26 09:15:00', tz=IST).timestamp())
    rec.on_tick({'symbol':'NSE:NIFTY50-INDEX','ltp':24300,'timestamp':base,'vol_traded_today':100})
    rec.on_tick({'symbol':'NSE:NIFTYTESTCE','ltp':100,'timestamp':base,'vol_traded_today':10})
    rec.on_tick({'symbol':'NSE:NIFTYTESTPE','ltp':200,'timestamp':base,'vol_traded_today':20})
    rec.set_oi_snapshot('NSE:NIFTYTESTCE', oi=1200, oi_change=50, prev_oi=1150)
    rec.set_oi_snapshot('NSE:NIFTYTESTPE', oi=900, oi_change=-30, prev_oi=930)
    next_min = base + 60
    rec.on_tick({'symbol':'NSE:NIFTY50-INDEX','ltp':24305,'timestamp':next_min,'vol_traded_today':110})
    rec.on_tick({'symbol':'NSE:NIFTYTESTCE','ltp':105,'timestamp':next_min,'vol_traded_today':15})
    rec.on_tick({'symbol':'NSE:NIFTYTESTPE','ltp':195,'timestamp':next_min,'vol_traded_today':25})
    rec.flush_all()
    rec.stop(flush=False)

    assert len(store.writes) == 6
    first_minute = [r for r in store.writes if r['candle_start'].endswith('09:15:00+05:30')]
    assert len(first_minute) == 3
    by_symbol = {r['symbol']: r for r in first_minute}
    assert by_symbol['NSE:NIFTY50-INDEX']['close'] == 24300
    assert by_symbol['NSE:NIFTYTESTCE']['high'] == 100
    assert by_symbol['NSE:NIFTYTESTCE']['oi'] == 1200
    assert by_symbol['NSE:NIFTYTESTCE']['oi_change'] == 50
    assert by_symbol['NSE:NIFTYTESTPE']['close'] == 200


def test_unknown_symbol_never_enters_recorder():
    store = FakeStore()
    meta = {'NSE:NIFTY50-INDEX': {'symbol':'NSE:NIFTY50-INDEX','underlying':'NIFTY','expiry':None,'strike':None,'option_type':None}}
    rec = CloudCandleRecorder(store, meta)
    rec.start()
    rec.on_tick({'symbol':'NSE:FOREIGN','ltp':1800,'timestamp':1787730000})
    rec.flush_all()
    rec.stop(flush=False)
    assert store.writes == []


def test_cloud_selection_records_every_ce_pe_returned_by_fyers():
    from engine import TradingEngine

    chain = []
    for strike in range(23000, 26050, 50):
        chain.append({"symbol": f"CE{strike}", "option_type": "CE", "strike_price": strike})
        chain.append({"symbol": f"PE{strike}", "option_type": "PE", "strike_price": strike})
    chain.append({"symbol": "NIFTY", "option_type": "", "ltp": 24230})

    meta, selected, items, chain_min, chain_max = TradingEngine._select_cloud_option_universe(
        chain,
        24230,
        {"expiry": "2026-08-27"},
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTY50-INDEX",
    )

    ce_strikes = sorted(meta[s]["strike"] for s in selected if meta[s]["option_type"] == "CE")
    pe_strikes = sorted(meta[s]["strike"] for s in selected if meta[s]["option_type"] == "PE")

    assert len(ce_strikes) == 61
    assert len(pe_strikes) == 61
    assert len(items) == 122
    assert ce_strikes[0] == 23000 and ce_strikes[-1] == 26000
    assert pe_strikes[0] == 23000 and pe_strikes[-1] == 26000
    assert chain_min == 23000
    assert chain_max == 26000


def test_recorder_can_roll_active_universe_without_deleting_history_registry():
    store = FakeStore()
    meta = {
        "A": {"symbol": "A", "underlying": "NIFTY", "expiry": "x", "strike": 100, "option_type": "CE"},
        "B": {"symbol": "B", "underlying": "NIFTY", "expiry": "x", "strike": 50, "option_type": "PE"},
    }
    rec = CloudCandleRecorder(store, meta)
    rec.start()
    base = int(pd.Timestamp("2026-08-26 09:15:00", tz=IST).timestamp())
    rec.on_tick({"symbol": "A", "ltp": 10, "timestamp": base})
    rec.deactivate_instruments(["A"])
    rec.register_instruments({
        "C": {"symbol": "C", "underlying": "NIFTY", "expiry": "x", "strike": 150, "option_type": "CE"}
    })
    rec.on_tick({"symbol": "A", "ltp": 99, "timestamp": base + 10})
    rec.on_tick({"symbol": "C", "ltp": 20, "timestamp": base + 10})
    rec.on_tick({"symbol": "C", "ltp": 21, "timestamp": base + 60})
    rec.flush_all()
    rec.stop(flush=False)

    symbols = [r["symbol"] for r in store.writes]
    assert "A" in symbols
    assert "C" in symbols
    a_rows = [r for r in store.writes if r["symbol"] == "A"]
    assert a_rows[0]["close"] == 10
    assert rec.active_symbols() == {"B", "C"}
