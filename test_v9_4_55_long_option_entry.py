from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_live_option_entry_is_buy_only():
    engine = (ROOT / "engine.py").read_text(encoding="utf-8")
    # The live/test/paper option entry order must always be broker BUY (1).
    assert 'side=1,' in engine
    assert 'side=1 if signal["side"] == "BUY" else -1' not in engine


def test_bearish_signal_maps_to_pe_without_sell_entry():
    client = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert 'wanted_type = "CE" if side == "BUY" else "PE"' in client
    engine = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert '"side": "BUY"' in engine
    assert '"signal_side": signal_side' in engine
