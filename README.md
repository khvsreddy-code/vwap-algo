# FYERS VWAP Trader V9.1 — Streamlit Live + Paper

## Run
`streamlit run app.py`

## What changed in V9.1
- Keeps `app.py` as the only Streamlit entry point.
- Uses **one persistent FYERS Data WebSocket** for NIFTY and the selected option.
- Uses FYERS **Lite mode** by default for the live feed. The strategy still gets historical candles/VWAP from REST, while the live strategy and paper P&L only need LTP ticks. This reduces bandwidth and Streamlit load.
- Dynamically subscribes the selected CE/PE on the existing socket instead of opening a second socket.
- Routes NIFTY and option ticks separately so option ticks cannot overwrite the NIFTY chart/strategy price.
- Stops the tight reconnect/error loop when FYERS returns subscription error `11011`.
- Suppresses repeated websocket error toasts/red boxes that were making the Streamlit page lag.
- Avoids refetching the option's historical chart on every fragment refresh.
- Reduces chart history payload from 300 to 120 bars; the browser-side Lightweight Charts instance updates the last candle in place.
- Paper trading remains local simulation only; it never calls the FYERS order endpoint.

## About FYERS error 11011
`11011 subscription failed` is a **server-side subscription rejection**, not a Streamlit chart-rendering error. FYERS examples use `FyersDataSocket` with an `appid:access_token` token and subscribe after the socket's authenticated connect callback. The app now follows that pattern and no longer loops aggressively when the server rejects the subscription.

If 11011 still appears after this update, verify that:
1. The App ID and today's access token belong to the same FYERS API app.
2. The token is a current v3 access token.
3. Market-data access is enabled for the account/app.
4. The symbol is valid for the account/feed.

The UI will show the rejection once and stop reconnecting so the page remains responsive. Press **Connect** again after correcting credentials/access.


## V9.2 stability patch
- Uses one long-lived FYERS market-data socket with the SDK's `reconnect=True`.
- Does not create replacement sockets on every transient disconnect.
- Suppresses repetitive "Connection to remote host was lost" events from the Streamlit UI.
- Shows transient transport loss only as a compact `RECONNECTING` header state.
- Re-subscribes the selected option after FYERS reconnects.
- Keeps the 11011 subscription rejection as a manual-action error.


## V9.3 performance/API hardening
- Reviewed against the current official FYERS v3 endpoint, market-data, authentication, symbol, order and WebSocket references.
- Uses a single long-lived `FyersDataSocket` with FYERS SDK `reconnect=True`; no tight application-level reconnect loop.
- Uses full `SymbolUpdate` by default so `vol_traded_today` can be used when the feed supplies it; the engine never fabricates volume.
- Sets the SDK queue processing interval to 50 ms when supported.
- Avoids duplicate option subscriptions and re-subscribes dynamically added option symbols after reconnect.
- Suppresses transport disconnect events from the Streamlit event queue.
- Removes per-tick/per-event browser toasts and hides the large event table unless requested.
- Loads funds/positions/holdings/orders/trades concurrently at connect to reduce startup latency.
- Option-chain selection requests greeks only when needed (selection path uses `greeks=0`).
- Reduces browser chart payload to the latest 90 candles while preserving in-place last-bar updates.
- Paper trading remains completely local and never calls the FYERS order endpoint.
