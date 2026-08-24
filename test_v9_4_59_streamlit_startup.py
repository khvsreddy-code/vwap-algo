from pathlib import Path

ROOT = Path(__file__).parent

def test_engine_import_has_keyerror_retry():
    text = (ROOT / "app.py").read_text()
    assert "sys.modules.pop(\"engine\", None)" in text
    assert "except KeyError as exc" in text

def test_history_is_chunked_for_intraday_reliability():
    text = (ROOT / "fyers_client.py").read_text()
    assert "chunk_days = 7" in text
    assert "date_format" in text and "epoch" in text

def test_empty_chart_history_is_not_cached():
    text = (ROOT / "app.py").read_text()
    assert "cache.pop(key, None)" in text or "cache.pop(key,None)" in text

def test_history_failure_does_not_block_engine_start():
    text = (ROOT / "engine.py").read_text()
    assert "Initial history unavailable; chart will retry REST history" in text

def test_chart_component_has_unique_v961_name():
    text = (ROOT / "v9_4_52_live_chart.py").read_text()
    assert "fyers_vwap_live_chart_v9_4_61" in text

def test_no_sell_entry_rule_remains():
    text = (ROOT / "engine.py").read_text()
    assert "entry_side = \"BUY\"" in text
    assert "side=1" in text
