from pathlib import Path

SRC = Path(__file__).with_name("v9_4_52_live_chart.py").read_text(encoding="utf-8")


def test_portaled_chart_hides_when_its_tab_slot_is_hidden():
    assert "host.style.display = " in SRC
    assert 'host.style.display = "none";' in SRC
    assert 'slot.style.visibility = "hidden";' not in SRC
    assert 'slot.style.opacity = "0";' in SRC
    assert "getComputedStyle(slot)" in SRC
    assert "slotIntersectionObserver" in SRC
    assert "slotMutationObserver" in SRC


def test_portaled_chart_is_removed_on_component_unmount():
    assert "if (state.host && state.host.isConnected) state.host.remove();" in SRC
    assert "delete registry[chartKey];" in SRC
