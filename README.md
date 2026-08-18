# FYERS VWAP Trading Terminal

Streamlit trading terminal built around FYERS API v3 and the supplied PineScript VWAP confirmation logic.

## What changed

- Fixed FYERS WebSocket authentication format: `APP_ID:ACCESS_TOKEN`.
- Added a proper v3 OAuth helper in the UI to generate the login URL and exchange `auth_code` for a fresh daily access token.
- Connection now calls `get_profile()` so authentication is tested before starting the engine.
- Added FYERS funds, positions, holdings, orderbook and tradebook panels.
- Added a candlestick chart rendered from FYERS OHLC/history data with VWAP, LTP and matching position levels.
- Added live engine status and strategy-state panels.
- Added Plotly chart support.
- Live orders remain disabled by default.

## FYERS authentication

FYERS v3 access tokens are time-limited. Error `-16` means the current token is not accepted/has expired. Generate a fresh token and make sure the App ID is the same API app that created that token.

The app supports:

1. Paste an already-generated access token, or
2. Open the FYERS v3 login URL from the sidebar, authorize, paste the returned `auth_code`, and exchange it for an access token.

The registered Redirect URI must exactly match the one entered in the FYERS API app.

## Run

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Chart

The chart is not a screenshot/iframe of the FYERS website. It is a native Streamlit/Plotly chart built from FYERS historical OHLC data and live FYERS ticks, so the bot can overlay its own VWAP, LTP and position levels.

## Strategy

The Python engine mirrors the supplied PineScript:

- OHLC4 source
- session VWAP
- cross only on a closed candle
- save VWAP at the cross
- confirmation only when a later closed candle closes at least `confirmationPoints` above/below the cross price
- confirmation window `confirmationBars`
- one active trade at a time

## Important

If `NSE:NIFTY50-INDEX` has zero/non-meaningful volume in FYERS, a true volume VWAP cannot be reproduced from that feed. Use a traded futures instrument for volume VWAP or use TradingView as the signal source.

Do not enable live trading until you have verified signal parity, position reconciliation and risk controls.
