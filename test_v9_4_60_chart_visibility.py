import re
from pathlib import Path

CHART = Path(__file__).with_name("v9_4_52_live_chart.py")

def test_chart_has_real_init_function():
    s = CHART.read_text()
    assert re.search(r"(?m)^\s*function init\(\)\s*\{", s)

def test_chart_initializes_canvas_before_optional_cdn():
    s = CHART.read_text()
    assert "Render the dependency-free canvas chart immediately" in s
    assert s.index("initFallback();") < s.index("loadScript().then(() => {")

def test_chart_does_not_wait_forever_on_stale_script():
    s = CHART.read_text()
    assert 'existing.dataset.vwapStatus || ""' in s
    assert "existing.remove()" in s
    assert "chart library timeout" in s

def test_chart_never_replaces_fallback_with_blank_error():
    s = CHART.read_text()
    assert "Canvas fallback is already active." in s
    assert 'host.textContent = "Unable to load the chart library."' not in s
