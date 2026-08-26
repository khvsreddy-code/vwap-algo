from pathlib import Path

ROOT = Path(__file__).resolve().parent


def test_market_socket_routes_only_explicit_symbols():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert 'if symbol == self.signal_symbol:' in src
    assert 'elif self.selected_option and symbol == str(self.selected_option.get("symbol") or ""):' in src
    assert 'if symbol == self.signal_symbol or not symbol:' not in src
    assert 'Unknown symbols are ignored. They must never mutate either chart.' in src


def test_option_tick_guard_blocks_foreign_scale_prices():
    src = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert 'ceiling = self._option_price_ceiling' in src
    assert 'if ceiling is not None and ltp > ceiling:' in src
    assert 'QUARANTINED OPTION TICK' in src


def test_option_chart_has_display_only_price_isolation():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'def _isolate_option_chart_prices(df):' in src
    assert 'ex = _isolate_option_chart_prices(ex)' in src
    assert 'This is a chart-only safety net' in src
