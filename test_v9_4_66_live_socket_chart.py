
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _text(name):
    return (ROOT / name).read_text()


def test_fyers_socket_does_not_use_finite_reconnect_retry():
    src = _text("fyers_client.py")
    assert "reconnect_retry=3" not in src
    assert "reconnect=True" in src


def test_live_chart_uses_engine_live_history_on_matching_timeframe():
    src = _text("app.py")
    assert "live_engine_df = engine.display_history()" in src
    assert 'if chart_timeframe == str(engine.resolution):' in src


def test_live_chart_has_update_sequence():
    src = _text("v9_4_52_live_chart.py")
    assert 'payload["updateSeq"]' in src
