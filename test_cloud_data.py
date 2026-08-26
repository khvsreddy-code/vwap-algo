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
