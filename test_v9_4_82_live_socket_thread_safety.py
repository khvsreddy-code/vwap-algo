from pathlib import Path

ROOT = Path(__file__).resolve().parent

def test_watchdog_never_invokes_socket_shutdown():
    s = (ROOT / "engine.py").read_text(encoding="utf-8")
    start = s.index("    def _socket_watchdog(self):")
    end = s.index("    def _run_socket(self):", start)
    block = s[start:end]
    assert "sock.close_connection()" not in block
    assert "sock.close()" not in block
    assert "MARKET_DATA_STALE_WAITING_FOR_SDK_RECONNECT" in block

def test_fyers_sdk_minimum_version():
    s = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "fyers-apiv3>=3.1.16" in s
