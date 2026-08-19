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


## FYERS Auth Web (Streamlit callback)

For the deployed app, use this exact Redirect URL in the FYERS API dashboard:

`https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/`

The flow is:

1. Open **Auth Web**.
2. Enter/verify App ID, Secret ID and the bare Redirect URI. The values are saved before the external link is opened.
3. Click **Open Auth Web**.
4. FYERS performs its normal authorization. If an active FYERS browser session is recognized, it can redirect straight back without showing the login form.
5. FYERS redirects directly to the **bare Streamlit root**:
   `/?s=ok&code=200&auth_code=...&state=...`
6. The app displays the one-time auth code on its callback screen.
7. Click **Back to Terminal**. The app exchanges the code exactly once and reconnects.

Do not register `/?page=auth` as the FYERS Redirect URI. The registered callback is the bare root URL above.

## FYERS Auth Web persistence (v9.3.4.5)

The Auth Web flow uses a short-lived random `state` ID. Before opening FYERS,
the app stores the App ID, Secret ID and Redirect URI in a short-lived local
server-side auth-flow record keyed by that state. FYERS returns the state with
the one-time `auth_code`, allowing a fresh Streamlit session to restore the
connection fields automatically.

The Secret ID is not placed in the browser URL. The auth-flow record expires
after 30 minutes and is consumed when the callback is received.

Recommended FYERS Redirect URL:
`https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/`


## v9.4.14 — Auth callback + historical backtest/replay

- FYERS callback URLs containing `?s=ok&code=200&auth_code=...&state=...` are treated as terminal callback pages.
- The one-time auth code is **not exchanged on callback arrival**. It is exchanged once when the user clicks **Back to Terminal**, using the App ID, Secret ID and Redirect URI saved before opening Auth Web.
- The callback URL is cleared only after the code has been consumed.
- Added a **Replay** page for historical VWAP strategy testing.
- Added a deterministic **Backtest** using the same `VwapConfirmationEngine` state machine as live trading.
- Replay/backtest entries are paper-only and never call a broker order endpoint.
- Historical chart errors are now surfaced instead of being silently swallowed.
- FYERS history parsing accepts both top-level and nested `data.candles` responses.


## v9.4.15 — auth link/state fix + historical chart fallback

This build fixes the two issues shown in the current UI:

### Auth
- **Open Auth Web always uses the current App ID + Secret ID + bare Redirect URI.**
- Editing any auth field automatically creates a **new random state and new auth URL**. The old URL cannot linger after credentials change.
- The values are saved **before** the external link is rendered.
- The auth link is a plain same-tab `<a target="_self">`, so the app does not intentionally open a second tab.
- FYERS redirects to the **bare Streamlit root**:
  `https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/`
- The callback screen is rendered before normal page routing and keeps the `auth_code` visible until **Back to Terminal**.
- The callback state restores App ID, Secret ID and Redirect URI even if Streamlit creates a fresh session.
- A **Start fresh auth attempt** control clears stale auth state.
- The auth page now contains the auth fields itself, so navigating to **Auth Web** no longer depends on a stale sidebar-generated link.

Important: the app cannot bypass FYERS authentication itself. If FYERS does not recognize an active FYERS browser session, FYERS may legitimately display its login page. When FYERS recognizes the session, the normal v3 authorization flow should redirect back to the registered callback without requiring a second login.

### Historical charts
- The Charts page now has a historical-only fallback when the live engine/websocket is not running.
- Historical candles can be loaded directly from FYERS and displayed with VWAP.
- The live chart has a **Reload historical chart** control to recover from an empty cached history state.

### Backtest / replay
- The existing backtest/replay continues to run the same VWAP confirmation state machine over historical candles.
- Replay entries are paper/simulation entries only; it never sends broker orders.
- Entry markers are shown on the replay chart so you can see exactly where the strategy would have entered.


## v9.4.16 — tab-persistent auth + deterministic backtest display

### Auth persistence
- App ID, Secret ID and Redirect URI are mirrored into browser `sessionStorage` before Auth Web is opened.
- `sessionStorage` survives the same-tab navigation from Streamlit → FYERS → Streamlit, so the fields are restored even when Streamlit creates a fresh session.
- The Secret ID is not placed in the FYERS URL. It is stored only for the lifetime of the browser tab and the short-lived server-side auth-flow record.
- Open Auth Web is rendered by a Streamlit Components V2 same-tab navigation component, which assigns `window.location.href` to the freshly generated v3 authorization URL. Copying the button link therefore copies the actual `generate-authcode` URL, not a stale callback URL.
- A fresh state is generated whenever App ID, Secret ID or Redirect URI changes.
- The FYERS callback remains the bare root URL and preserves `?s=ok&code=200&auth_code=...&state=...` until Back to Terminal.

### Backtest / replay
- Run full backtest now plots its entries and exits on the exact historical dataframe that was loaded.
- The results table and chart use the same immutable in-memory dataset; the full backtest does not fetch or generate a second/random dataset.
- Replay continues to advance through the same loaded candles and re-runs the exact VWAP confirmation state machine on the visible prefix.
- Replay entries are simulated/paper entries only and never send FYERS broker orders.
