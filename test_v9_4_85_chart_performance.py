from pathlib import Path
import ast

ROOT = Path(__file__).parent
APP = ROOT / "app.py"
CHART = ROOT / "v9_4_52_live_chart.py"

def test_chart_has_independent_one_second_fragment():
    src = APP.read_text(encoding="utf-8")
    assert '@st.fragment(run_every="1s")' in src
    assert "def _terminal_chart_live():" in src
    assert "_terminal_chart_live()" in src

def test_dashboard_keeps_two_second_outer_cadence():
    src = APP.read_text(encoding="utf-8")
    assert '@st.fragment(run_every="2s")' in src

def test_chart_has_incremental_python_fast_path():
    src = CHART.read_text(encoding="utf-8")
    assert "Fast path for the steady-state live tick" in src
    assert '"mode": "delta"' in src
    assert "row_count = min(len(df)" in src

def test_chart_browser_updates_are_coalesced():
    src = CHART.read_text(encoding="utf-8")
    assert "schedulePortalSync" in src
    assert "requestAnimationFrame" in src
    assert "Binary-search the nearest candle" in src
    assert "lastMarkerPayloadKey" in src

def test_sources_compile():
    ast.parse(APP.read_text(encoding="utf-8"))
    ast.parse(CHART.read_text(encoding="utf-8"))
