from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_wide_chain_does_not_bulk_backfill_option_history():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "Option contracts are intentionally NOT historical-backfilled here." in src
    assert 'self.client.history(symbol, "1", days=3, oi_flag=True)' not in src
    # Newly discovered contracts must be subscribed live without spawning a
    # historical REST request per symbol.
    roll = src.split("def _cloud_roll_worker", 1)[1].split("def _cloud_backfill_symbols", 1)[0]
    assert "fyers-cloud-chain-backfill" not in roll
    assert "subscribe_data_socket(sock, sorted(added), \"SymbolUpdate\")" in roll


def test_option_chain_refresh_is_throttled():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "_cloud_chain_refresh_cooldown_seconds = 30.0" in src
    assert "_cloud_last_chain_refresh_at" in src
    assert "elapsed < self._cloud_chain_refresh_cooldown_seconds" in src


def test_cloud_start_records_initial_chain_and_only_nifty_history_backfill():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    start = src.split("def _cloud_backfill_today", 1)[1].split("def _cloud_oi_loop", 1)[0]
    assert 'symbol = self.signal_symbol' in start
    assert 'self.client.history(symbol, "1", days=3, oi_flag=False)' in start
    assert 'symbols = [self.signal_symbol] + sorted(self.cloud_data_symbols)' not in start
