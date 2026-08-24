from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parent

def test_history_does_not_delete_single_candle():
    text = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "df.iloc[:-1]" not in text

def test_index_history_does_not_send_continuous_future_flag():
    text = (ROOT / "fyers_client.py").read_text(encoding="utf-8")
    assert 'is_continuous_future = "FUT" in str(symbol).upper()' in text
    assert 'date_request["cont_flag"] = "1"' in text

def test_chart_bootstraps_history_before_render():
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "if chart_df is None or chart_df.empty:" in text
    assert "seeded = engine.load_history(days=31)" in text

def test_all_python_sources_parse():
    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
