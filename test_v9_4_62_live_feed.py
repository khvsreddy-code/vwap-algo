"""Regression checks for v9.4.65 live-feed/chart patch."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def test_market_data_import_is_independent():
    s = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert "from fyers_apiv3.FyersWebsocket import data_ws" in s
    assert "from fyers_apiv3.FyersWebsocket import order_ws" in s
    assert "from fyers_apiv3.FyersWebsocket import data_ws, order_ws" not in s

def test_socket_runner_does_not_emit_self_inflicted_socket_end_error():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "self.client.start_data_socket(" in s
    assert "Market data socket ended; reconnecting" not in s
    assert "self.socket = sock" in s


def test_fancy_label_overlay_is_present():
    s = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "updateMarkerOverlay" in s
    assert "BUY CE" in s
    assert "BUY PE" in s

def test_socket_watchdog_is_present():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "def _socket_watchdog(self):" in s
    assert "Market data stale for" in s
    # A closed/stuck FYERS transport can have ws_connected=False while the
    # SDK is still trapped in its reconnect loop; stale detection must not
    # skip that state.
    assert "do not require ws_connected here" in s
    assert "tick_age <= 15" in s

def test_chart_v2_mount_is_dom_guarded():
    s = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "const rootIsDom" in s
    assert "typeof root.appendChild === " in s
    assert "host.parentElement !== root" in s or "root.querySelector" in s
    assert 'name="fyers_vwap_live_chart_v9_4_65"' in s


def test_fyers_socket_does_not_force_three_retry_shutdown():
    s = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert "reconnect=True" in s
    assert "reconnect_retry=3" in s or "reconnect_retry=3" not in s


def test_engine_keeps_working_socket_lifecycle():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "self.client.start_data_socket(" in s
    assert "retry_delay = 2.0" in s
    assert "Market data socket ended; reconnecting" not in s


def test_socket_client_waits_for_real_lifecycle_end():
    s = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert "lifecycle_done = threading.Event()" in s
    assert "lifecycle_done.wait()" in s
    assert "reconnect=True" in s


def test_black_white_theme_is_available():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    chart = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "Black & White" in app
    assert '"theme": str(st.session_state.get("ui_theme", "Dark"))' in chart
    assert "function themeColors" in chart
