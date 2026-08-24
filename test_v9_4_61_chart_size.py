from pathlib import Path

CHART = Path(__file__).with_name("v9_4_52_live_chart.py").read_text()


def test_chart_uses_streamlit_v2_parent_element_not_renderer_args_as_dom():
    assert 'const { parentElement, data = {}, key = "" } = component;' in CHART
    assert "const root = parentElement;" in CHART
    assert "root.appendChild(host)" in CHART
    assert "const root = component;" not in CHART
    assert "component.appendChild" not in CHART


def test_chart_sizes_from_streamlit_mount_host():
    assert "const mountHost = root.host instanceof HTMLElement ? root.host : root;" in CHART
    assert "getMountRect()" in CHART
    assert "mountRect.width" in CHART
    assert "mountRect.height" in CHART
    assert "state.resizeObserver.observe(mountHost)" in CHART


def test_chart_retries_resize_after_streamlit_layout_settles():
    assert "setTimeout(resizeChart, 80)" in CHART
    assert "setTimeout(resizeChart, 250)" in CHART


def test_chart_is_registered_as_v9_4_62():
    assert 'name="fyers_vwap_live_chart_v9_4_62"' in CHART


def test_chart_uses_custom_fancy_signal_label_overlay():
    assert "ensureMarkerOverlay()" in CHART
    assert "updateMarkerOverlay(markers)" in CHART
    assert "BUY CE / BUY PE labels" in CHART
    assert 'borderRadius = "6px"' in CHART
    assert "markerPalette(text)" in CHART
    assert "subscribeVisibleLogicalRangeChange(scheduleMarkerOverlayUpdate)" in CHART
    assert "state.candles.setMarkers(markerData)" not in CHART
