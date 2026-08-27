from pathlib import Path

SRC = Path(__file__).with_name('v9_4_52_live_chart.py').read_text()


def test_live_chart_component_key_is_stable():
    assert 'component_key = f"live-{title}-{int(height)}"' in SRC
    assert 'component_key = f"live-{title}-{int(height)}-{update_key}"' not in SRC


def test_live_chart_data_is_update_payload_not_component_identity():
    assert 'payload["updateSeq"]' in SRC
    assert 'The data payload is the update signal.' in SRC
