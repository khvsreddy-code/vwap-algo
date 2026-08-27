from pathlib import Path
import ast

ROOT = Path(__file__).parent

def test_page_switch_forces_full_chart_bootstrap():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '_previous_chart_page = st.session_state.get("_v952_chart_page")' in src
    assert 'st.session_state.pop("_v952_chart_signature", None)' in src

def test_terminal_merges_live_candle_by_timestamp():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'matches = chart_df["datetime"] == live_dt' in src
    assert 'sort_values("datetime")' in src

def test_engine_seeds_in_progress_candle_from_history():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert 'seeded = None' in src
    assert 'matches = dts == start' in src

def test_chart_normalizes_timestamp_buckets():
    src = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert 'def _normalise_chart_frame' in src
    assert 'groupby("datetime", sort=True)' in src
    assert 'resolution=None' in src
    assert 'state.candleData.sort((a, b) => Number(a.time) - Number(b.time))' in src

def test_sources_compile():
    for name in ("app.py", "engine.py", "fyers_client.py", "v9_4_52_live_chart.py"):
        ast.parse((ROOT / name).read_text(encoding="utf-8"))

print("v9.4.87 chart data integrity checks passed")
