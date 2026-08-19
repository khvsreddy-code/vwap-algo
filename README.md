# FYERS VWAP Trader

Streamlit trading terminal for the user's Pine VWAP confirmation strategy.

## Strategy
- Closed-candle OHLC4 vs session VWAP cross.
- After a bullish cross, wait for close >= cross VWAP + confirmation points.
- After a bearish cross, wait for close <= cross VWAP - confirmation points.
- Confirmation window is configurable.
- Once confirmed, BUY a NIFTY CE for bullish signals or BUY a NIFTY PE for bearish signals.
- Option is selected from FYERS option chain by LTP premium band (default ₹180–₹200, preferred ₹190).
- Expiry can be nearest or monthly.
- Protection can be Points, Percent, or ATR-derived.
- With LIVE TRADING enabled and protection enabled, the entry is submitted as an FYERS BO order carrying stopLoss/takeProfit fields.

## Important NIFTY VWAP note
A classic VWAP needs meaningful volume. If the FYERS NIFTY index feed returns zero volume, the terminal cannot reproduce a volume VWAP from that feed. Use a volume-bearing NIFTY futures symbol as the VWAP data source if that matches your TradingView reference.

## Run
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Authentication
Use the sidebar OAuth helper to generate the FYERS v3 authorization URL, authorize, copy the `auth_code` from the redirect, and exchange it for the daily access token. The App ID, Secret ID, and Redirect URI must match the FYERS API app.

## Live trading
LIVE TRADING is OFF by default. Validate signals, option selection, chart levels, and portfolio reconciliation in dry-run before enabling it. FYERS API order placement is subject to the current compliant API-app/static-IP requirements.

## Simplified login

The sidebar now has only **App ID** and **Access token** for normal use. The OAuth/token-generation steps are hidden under **Get a fresh access token** and are only needed when the daily token expires or FYERS returns error -16.
