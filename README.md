# FYERS VWAP Trader V9.4.48 — Streamlit Live + Paper

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


## v9.4.21 — replay chart TypeError hardening
- Sanitizes OHLC, VWAP, markers, levels, and LTP values before sending them to the Streamlit V2 chart component.
- Keeps the replay component instance stable while its candle payload grows from 1 → N candles.
- Preserves `max_candles=None` for replay/full backtest/historical views so a cursor of 1245 renders all 1245 revealed candles.
- Adds a compatibility fallback for Streamlit runtimes that reject V2 `width`/`height` mount arguments.


## V9.4.23 replay realism / marker fix
# Replay fix v9.4.23 — realistic entry/exit timing and marker placement

- Backtest exits are no longer evaluated on the same candle that generated the
  entry. The entry candle's OHLC cannot tell whether the SL/target happened
  before or after the intrabar confirmation, so exits start on the next candle.
- Replay now shows both ENTRY and EXIT markers.
- Trade markers are snapped to the nearest actual loaded candle timestamp,
  preventing timezone/precision differences from placing all markers at the
  chart edge/first candle.
- Marker arrays are sorted by candle time before being sent to Lightweight
  Charts.
- The backtest still uses only the historical FYERS dataframe already loaded;
  it does not generate a random dataset or fetch a second dataset.


## v9.4.31
- Configurable IST algo start/end window (default 09:15–15:15).
- First live/replay candle is explicitly marked at the configured session start and uses the full selected timeframe.
- Live candle bucketing is anchored to the configured session start.
- Replay and live use the same configurable session rules.
- Opening-candle VWAP crossing uses the candle's own OHLC rather than the prior day's close/VWAP.


## v9.4.38 — false-trigger guard + anti-blink chart + persistent markers

- NIFTY terminal and Charts views render the full 31-day loaded history and show historical BUY CE / BUY PE markers at the exact confirmation candles.
- Live execution markers remain persisted even when option selection/order handling changes the event status.
- The latest cross and confirmation levels remain visible after an entry instead of disappearing when the strategy clears its setup state.
- Selected option context is restored from the local execution ledger after reconnects/page switches.
- Selected option history is loaded for 31 days and the full loaded option history is rendered.
- New defaults: 15-point confirmation move, 8-candle confirmation window, and ₹170–₹210 premium band (preferred ₹190).


## v9.4.40 — Bidi chart payload + deployment recovery

- Keeps **31 days of 5-minute NIFTY history** for the chart and historical VWAP/signal calculation.
- Historical BUY/SELL option markers are calculated from the same confirmation state machine used by the live engine and are pinned to the exact confirmation candle.
- Live execution markers are persisted in the execution ledger so they do not disappear during Streamlit fragment reruns.
- The chart no longer recreates unchanged LTP/cross/trigger lines on every refresh, reducing blinking and disappearing levels.
- The complete chart payload is compressed with **gzip + base64** before it is sent through Streamlit Custom Components V2. The browser decodes it with `DecompressionStream`. This prevents oversized/truncated bidi payloads from surfacing as `BidiComponent Error: Unexpected end of input`.
- The chart renderer JavaScript was syntax-checked and the Python modules were compile/import tested.
- Strategy defaults remain **15 points confirmation** and **8 candles**. The crossing candle only arms the setup; it cannot confirm itself. For example, a BUY cross close at 24244 requires the confirmation level 24259 to be reached; a following candle reaching only 24250 must not trigger.
- `app.py` is the single Streamlit entry point and must remain at the **repository root**. Deploy the complete root-level source set to the Git branch configured in Streamlit Cloud.
- Run locally with:
  `streamlit run app.py`

### Deployment recovery

If Streamlit reports:

`Main module does not exist`

or:

`FileNotFoundError: /mount/src/vwap-algo/app.py`

the deployed GitHub revision is missing the root-level `app.py` or Streamlit is pointed at the wrong file. Push the complete source set to the configured branch and set the Streamlit Main file to `app.py`, then reboot/redeploy.

This release intentionally keeps this README as the **only Markdown documentation file** in the source package; release-specific notes are consolidated here rather than creating additional `FIX_NOTES_*.md` or recovery Markdown files.

## V9.4.43 live-chart stability
- Replaced the gzip/base64 chart transport with bounded ordinary JSON.
- Live terminal charts use a rolling 720-candle window instead of retransmitting the full month on every fragment rerun.
- The browser-side Lightweight Charts instance remains mounted and updates candles, VWAP, levels, and BUY/SELL CE/PE markers in place.
- Live viewport fitting is performed only when necessary, preventing the rapid chart blinking caused by repeated full refits.
- The versioned chart module is `v9_4_43_live_chart.py`.


## v9.4.44 — reliability and regression hardening

- Historical strategy seeding now replays confirmation/expiry state instead of leaving a setup armed after that setup already confirmed in history.
- Live tick confirmation is session-aware and cannot fire a previous trading day's VWAP setup on the next session's first tick.
- Reconnecting the market-data socket rebuilds only the partial OHLC/cumulative-volume state; the valid strategy setup is preserved.
- Removed the obsolete `live_chart.py` test reference; regression tests now validate the versioned live-chart module.
- Removed the deprecated `use_container_width` Streamlit API usage in favor of `width="stretch"`.
- Removed an unreachable duplicate execution-event UI branch.

## v9.4.45 — Terminal stability and execution-loop efficiency

- Terminal UI refresh cadence is now 2 seconds. FYERS websocket market-data and strategy evaluation remain tick-driven, so this does **not** slow the trading engine.
- The live chart is no longer forced through a 1-second terminal rerun cadence, reducing visible chart blinking and unnecessary browser/component work.
- The NIFTY strategy fallback no longer rebuilds the complete history + VWAP DataFrame on every market tick. Closed-candle VWAP/cross processing now runs only when a candle actually rolls over.
- Raw LTP confirmation remains tick-driven, so an armed 15-point setup can still trigger immediately when the live price reaches its confirmation level.
- This reduces pandas allocations, VWAP recalculation, Streamlit rerender pressure, and websocket callback work during active markets.
- The versioned chart module is `v9_4_45_live_chart.py`.
- This package intentionally contains only this original `README.md`; no additional Markdown files were added.


## v9.4.46 — FYERS v3 streaming and Terminal efficiency

- Terminal chart updates use an incremental payload after the initial historical
  load: the browser receives the latest candle/VWAP point, LTP, levels and
  execution markers instead of retransmitting the complete rolling history on
  every UI update.
- The Lightweight Charts instance remains persistent and only updates the
  existing series.
- Market-data processing remains tick-driven and is independent of the UI
  cadence.
- Added the dedicated FYERS API v3 Order WebSocket for real-time order, trade
  and position events. This removes the blocking REST portfolio refresh that
  previously ran after every live execution.
- Kept FYERS market-data WebSocket for the strategy. The strategy's
  confirmation check remains tick-level; closed-candle VWAP preparation is
  performed only when a candle rolls over.
- Added bounded order-websocket event state for the Terminal.
- Updated tests to reference the current versioned chart module.


## v9.4.47 performance/reliability improvements

- Market-data websocket processing no longer waits for option selection/order REST calls; live entries are handed to a dedicated execution worker.
- Historical chart data is cached in-session by symbol/timeframe/window and is fetched again only on an explicit historical-chart reload.
- The persistent browser chart continues to use incremental candle updates rather than retransmitting the full rolling dataset.
- Existing 15-point / 8-candle confirmation behavior is preserved.
- Streamlit UI refresh cadence is intentionally separate from market-data and execution cadence; the UI is not part of the order-critical path.


## V9.4.48 — Order WebSocket + Replay Entry/Marker Fixes
- Initializes the dedicated FYERS v3 Order WebSocket state before first use, fixing the `TradingEngine` `start_order_socket` AttributeError.
- Keeps order/trade/position WebSocket state separate from the market-data WebSocket.
- Fixes live candle rollover detection to use the engine's actual `_current_candle` state.
- Replay now uses the same configured 15-point / 8-candle confirmation rules and the currently selected algo start/end time for both full backtest and step-by-step replay.
- Historical replay confirmation is intrabar-aware: a BUY confirmation is taken when candle high reaches the configured trigger and a SELL confirmation when candle low reaches it, matching the live strategy's tick-level confirmation behavior as closely as OHLC history permits.
- Replay execution records now retain cross price, confirmation level, and trigger price so an entry can be audited instead of appearing as an unexplained trade.
- Every replay-confirmed entry receives a persistent `BUY CE` or `BUY PE` chart marker on the candle where the confirmation occurred.
- The original README remains the only Markdown documentation file.


## v9.4.49 — VWAP rounded-candle interaction

- Keeps the existing 15-point confirmation and 8-candle confirmation window.
- Adds a guarded `VWAP_INTERACTION` setup for small-body/doji/rounded candles
  that genuinely straddle VWAP.
- A VWAP interaction only arms the setup; it does not bypass confirmation.
- Pure wick touches that close away from VWAP are still ignored.
- Replay and live strategy paths use the same detection helper, so rounded
  VWAP interactions behave consistently in both modes.
- Execution logs identify whether a setup came from `CLOSE_CROSS` or
  `VWAP_INTERACTION`.
