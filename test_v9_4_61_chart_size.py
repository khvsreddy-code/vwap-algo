from pathlib import Path

CHART = Path(__file__).with_name("v9_4_52_live_chart.py").read_text()
APP = Path(__file__).with_name("app.py").read_text()


def test_chart_sizes_from_v2_component_not_narrow_parent():
    assert "const root = component;" in CHART
    assert "component.clientWidth" in CHART
    assert "component.clientHeight" in CHART
    assert "state.resizeObserver.observe(component)" in CHART


def test_chart_retries_resize_after_streamlit_layout_settles():
    assert "setTimeout(resizeChart, 80)" in CHART
    assert "setTimeout(resizeChart, 250)" in CHART


def test_chart_stays_full_width_in_python_mount():
    assert 'width="stretch"' in CHART
    assert 'height=int(height)' in CHART
