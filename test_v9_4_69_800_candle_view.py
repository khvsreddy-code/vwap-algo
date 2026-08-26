from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_live_chart_defaults_to_800_candles_and_two_day_initial_view():
    chart = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    assert "DEFAULT_CHART_CANDLES = 800" in chart
    assert "initialVisibleRange" in chart
    assert "setVisibleRange(d.initialVisibleRange)" in chart
    assert "state.initialViewApplied" in chart
    assert "while (candles.length > DEFAULT_CHART_CANDLES)" in chart


def test_app_requests_enough_history_for_800_bars():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "CHART_CANDLE_LIMIT = 800" in app
    assert "def _chart_history_days" in app
    assert "_merge_chart_history" in app
    assert "max_candles=CHART_CANDLE_LIMIT" in app


def test_initial_view_is_not_reset_on_each_live_payload():
    chart = (ROOT / "v9_4_52_live_chart.py").read_text(encoding="utf-8")
    block = chart[chart.index('if (d.mode !== "delta" && !state.initialViewApplied)'):chart.index('state.lastPayload = d;', chart.index('if (d.mode !== "delta" && !state.initialViewApplied)'))]
    assert "setVisibleRange" in block
    assert "state.initialViewApplied = true" in block
