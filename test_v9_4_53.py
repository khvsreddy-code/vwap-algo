"""Regression coverage for backtest session-risk accounting.

The Streamlit module also contains UI startup code, so this test loads only
the pure replay helpers from its AST and exercises them without a browser.
"""
import ast
from datetime import time as dt_time
from pathlib import Path

import pandas as pd

from strategy import StrategyConfig, VwapConfirmationEngine


def _load_backtest():
    source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_ist_timestamp", "_hhmm", "_hhmm_minute", "_within_entry_window",
        "option_lot_size_for_symbol", "_run_backtest",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {
        "pd": pd, "REPLAY_IST": "Asia/Kolkata", "DEFAULT_ENTRY_START_MINUTE": 555,
        "DEFAULT_ENTRY_END_MINUTE": 915, "VwapConfirmationEngine": VwapConfirmationEngine,
        "StrategyConfig": StrategyConfig,
        "INDEX_OPTION_LOT_SIZES": {"NIFTY50": 65, "NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "NIFTYNXT50": 25},
    }
    exec(compile(module, "app_backtest_helpers", "exec"), namespace)
    return namespace["_run_backtest"]


def _bar(timestamp, open_, high, low, close):
    return {"datetime": pd.Timestamp(timestamp, tz="Asia/Kolkata"), "open": open_, "high": high,
            "low": low, "close": close, "vwap": 100.0, "prev_close": None, "prev_vwap": None,
            "atr": 5.0, "adx": 30.0, "vwap_slope": 1.0}


def test_positions_flatten_at_configured_session_close():
    run_backtest = _load_backtest()
    # Avoid indicator recomputation here; this test targets session close and
    # financial accounting after the strategy has produced a confirmed signal.
    original_prepare = VwapConfirmationEngine.prepare
    VwapConfirmationEngine.prepare = staticmethod(lambda df: df.copy())
    try:
        df = pd.DataFrame([
            _bar("2026-08-06 15:10", 99, 102, 99, 101),  # arm BUY
            _bar("2026-08-06 15:15", 101, 103, 100, 102), # confirm BUY
            _bar("2026-08-06 15:25", 102, 104, 101, 103), # mandatory flat
        ])
        result = run_backtest(df, 1, 2, session_start=dt_time(15, 10), session_end=dt_time(15, 15),
                              session_flat_time=dt_time(15, 25), quantity=65, estimated_cost_per_trade=10)
    finally:
        VwapConfirmationEngine.prepare = original_prepare

    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["reason"] == "SESSION_CLOSE"
    assert trade["exit_time"].strftime("%H:%M") == "15:25"
    assert trade["net_pnl_rupees"] == trade["pnl"] * 65 - 10
    assert result["max_drawdown_points"] >= 0


if __name__ == "__main__":
    test_positions_flatten_at_configured_session_close()
    print("v9.4.53 backtest safety tests passed")
