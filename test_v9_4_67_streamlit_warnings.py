from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _text(name):
    return (ROOT / name).read_text()


def test_chart_timeframe_does_not_seed_session_state_and_pass_index_together():
    src = _text("app.py")
    assert 'st.session_state.chart_timeframe =' not in src
    assert 'key="chart_timeframe"' in src
    assert 'chart_default_index' in src


def test_live_chart_component_registration_is_cached():
    src = _text("v9_4_52_live_chart.py")
    assert '@st.cache_resource(show_spinner=False)' in src
    assert 'def _register_live_chart_component()' in src
    assert 'name="fyers_vwap_live_chart_v9_4_65"' in src
