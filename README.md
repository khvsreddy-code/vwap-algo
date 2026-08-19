# FYERS VWAP Trader V9 — Streamlit

This is the V5 project kept flat in one directory, with the V7 TradingView-style live status/chart work and V8 local paper trading integrated.

## Run
`streamlit run app.py`

The project contains no nested project folders.

## Paper trading
Paper BUY/SELL uses the live FYERS market-data price but never calls `place_order()`.
SL/target are checked against every incoming tick. Paper history is kept in the current Streamlit session.

## Live strategy
The existing V5 engine remains responsible for:
- closed-candle VWAP cross
- configurable 10/15 point confirmation
- confirmation window
- NIFTY CE/PE selection in the configured ₹180–₹200 premium band
- protection configuration

LIVE orders remain off by default.
