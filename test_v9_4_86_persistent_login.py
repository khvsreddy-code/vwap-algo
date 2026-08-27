import ast
from pathlib import Path

APP = Path(__file__).with_name("app.py")
SRC = APP.read_text(encoding="utf-8")


def test_persistent_session_store_exists():
    assert "FYERS_SESSION_FILE" in SRC
    assert "def _load_saved_fyers_session" in SRC
    assert "def _save_fyers_session" in SRC


def test_token_is_restored_before_session_state_defaults():
    assert '"token": _saved_token' in SRC


def test_refresh_auto_reconnect_is_one_shot():
    assert '"_auto_reconnect_attempted": False' in SRC
    assert "not st.session_state.get(\"_auto_reconnect_attempted\", False)" in SRC
    assert "st.session_state._auto_reconnect_attempted = True" in SRC


def test_successful_connection_persists_current_token():
    assert "_save_fyers_session(app_id, token)" in SRC


def test_token_is_not_put_in_query_params():
    # Persistence must remain server-side; don't regress into putting the
    # sensitive FYERS token into the browser URL.
    assert 'query_params["token"]' not in SRC
    assert 'query_params["access_token"]' not in SRC
