"""Regression checks for v9.4.62 live-feed/chart patch."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def test_market_data_import_is_independent():
    s = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert "from fyers_apiv3.FyersWebsocket import data_ws" in s
    assert "from fyers_apiv3.FyersWebsocket import order_ws" in s
    assert "from fyers_apiv3.FyersWebsocket import data_ws, order_ws" not in s

def test_socket_runner_retries_when_sdk_returns():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "while self.running and not self.market_data_blocked:" in s
    assert "Market data socket ended; reconnecting" in s

def test_fancy_label_overlay_is_present():
    s = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "updateMarkerOverlay" in s
    assert "BUY CE" in s
    assert "BUY PE" in s

def test_socket_watchdog_is_present():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "def _socket_watchdog(self):" in s
    assert "Market data stale for" in s

def test_chart_v2_mount_is_dom_guarded():
    s = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "const rootIsDom" in s
    assert "typeof root.appendChild === " in s
    assert "host.parentElement !== root" in s or "root.querySelector" in s
    assert 'name="fyers_vwap_live_chart_v9_4_62"' in s
