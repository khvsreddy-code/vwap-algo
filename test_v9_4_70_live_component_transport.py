from pathlib import Path


def test_live_component_uses_stable_python_key_and_payload_updates():
    src = Path(__file__).with_name("v9_4_52_live_chart.py").read_text()
    assert 'component_key = f"live-{title}-{int(height)}"' in src
    assert 'component_key = f"live-{title}-{int(height)}-{update_key}"' not in src
    assert 'payload["updateSeq"]' in src


def test_payload_has_stable_chart_identity_separate_from_update_sequence():
    src = Path(__file__).with_name("v9_4_52_live_chart.py").read_text()
    assert '"componentKey": f"live-{title}-{int(height)}"' in src
