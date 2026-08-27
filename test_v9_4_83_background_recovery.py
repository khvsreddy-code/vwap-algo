"""Regression tests for v9.4.83 background EOD recovery.

These tests are intentionally source-level so they can run without Streamlit
Cloud credentials or a live FYERS account.
"""
from pathlib import Path
import ast


ROOT = Path(__file__).parent
APP = ROOT / "app.py"


def _source():
    return APP.read_text(encoding="utf-8")


def test_recovery_manager_is_background_and_daemon():
    src = _source()
    assert "class _RecoveryManager:" in src
    assert "threading.Thread(" in src
    assert "daemon=True" in src
    assert "target=self._run" in src
    assert "@st.cache_resource(show_spinner=False)" in src


def test_worker_never_writes_streamlit_progress():
    src = _source()
    start = src.index("def _build_day_recovery_rows(")
    end = src.index("\n\ndef _store_insert_missing_candles", start)
    body = src[start:end]
    assert "st.progress" not in body
    assert "st.empty" not in body
    assert "st.spinner" not in body


def test_data_center_starts_recovery_instead_of_waiting_for_it():
    src = _source()
    start = src.index("def _render_data_center():")
    end = src.index("\nif page == \"data\":", start)
    body = src[start:end]
    assert "manager.start(" in body
    assert "@st.fragment(run_every=\"1s\")" in body
    assert "manager.snapshot(" in body
    assert "with st.spinner(\"Fetching FYERS history" not in body


def test_recovery_uses_store_fetch_instruments_and_separate_clients():
    src = _source()
    assert "def _store_fetch_instruments(store):" in src
    manager_start = src.index("class _RecoveryManager:")
    manager_end = src.index("\ndef _build_day_recovery_rows", manager_start)
    manager = src[manager_start:manager_end]
    assert "store = CloudMarketStore(url, key)" in manager
    assert "client = FyersClient(app_id, token)" in manager


def test_app_compiles():
    ast.parse(_source())
