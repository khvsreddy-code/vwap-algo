import ast
from pathlib import Path

ROOT = Path(__file__).parent
app = ast.parse((ROOT / "app.py").read_text())
source = (ROOT / "app.py").read_text()
assert "def _store_fetch_instruments(store):" in source
assert "def _store_insert_missing_candles(store, rows):" in source
assert "store.client.table(\"instruments\")" in source
assert "on_conflict=\"symbol,candle_start\"" in source
assert "_store_fetch_instruments(store)" in source
assert "_store_insert_missing_candles(store, all_rows)" in source
print("v9.4.79 EOD compatibility checks passed")
