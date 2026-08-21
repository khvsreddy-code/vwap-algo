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
