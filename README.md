# FYERS VWAP Entry Engine

This project converts the supplied PineScript's VWAP confirmation/entry logic into Python and gives it a Streamlit UI.

## Strategy parity

The supplied PineScript uses:

- VWAP source: OHLC4
- Cross only on a confirmed/closed candle
- Store VWAP at the cross as `crossPrice`
- Confirm LONG when a later closed candle has `close >= crossPrice + confirmationPoints`
- Confirm SHORT when a later closed candle has `close <= crossPrice - confirmationPoints`
- Confirmation window: up to `confirmationBars` candles
- After confirmation, the setup is cleared

The Python engine mirrors those rules.

## Install

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

Enter your FYERS App ID and access token in the sidebar.

## Important FYERS notes

- Use FYERS API v3.
- FYERS' current API rules require the compliant API app setup and validated static-IP arrangement for order placement where applicable. Verify your account/app is eligible before enabling live orders.
- The data socket uses the official Python SDK's `FyersDataSocket` and `SymbolUpdate` stream.
- The app defaults to dry-run. Keep LIVE TRADING off until signal parity is verified.

## VWAP caveat for NIFTY index

Your PineScript uses `ta.vwap(ohlc4)`, which is volume-weighted. A cash index can have no meaningful traded volume in the broker feed. If FYERS returns zero volume for `NSE:NIFTY50-INDEX`, this Python app cannot reproduce a true volume VWAP from that instrument alone.

For exact volume VWAP, use a traded instrument such as the relevant NIFTY futures contract, or use TradingView as the signal source and FYERS only as the execution broker.

## Production hardening still needed

Before live deployment, add:

- persistent state/database so a restart cannot duplicate an entry
- order-websocket reconciliation for fills/positions
- hard daily loss and max-trade limits
- duplicate-signal protection
- exchange-hours checks
- option-contract selection if execution is through NIFTY CE/PE
- proper stop-loss/target order management
- structured logs and alerts
- static-IP deployment suitable for your FYERS API setup
