from pathlib import Path

src = Path('v9_4_52_live_chart.py').read_text()
marker = 'if (state.chart || state.fallback) {'
assert marker in src
pos = src.index(marker)
assert 'try { applyPayload(data); }' in src[pos:pos+500]
# The renderer must contain an applyPayload(data) call after the renderer setup,
# not only inside init(), so subsequent Streamlit data updates reach the chart.
assert src.count('applyPayload(data)') >= 4
print('live renderer update bridge: PASS')
