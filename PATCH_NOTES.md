# v9.6.6 — live-feed fallback / stuck CONNECTING fix

- Added a low-frequency REST quote fallback for NIFTY when the FYERS websocket stops delivering ticks for more than ~5 seconds.
- The websocket remains the primary transport; REST fallback is only activated during a stale period and is capped at roughly one quote every 3 seconds.
- Fallback LTPs feed the same live candle/strategy path, so the chart keeps moving and the 15-point confirmation engine does not freeze while the SDK reconnects.
- Completed candles missed during the websocket stall still use the existing asynchronous FYERS History gap-repair path.
- The UI now reports `LIVE • REST FALLBACK` instead of misleading `CONNECTING` while the emergency quote bridge is keeping live data flowing.
- The FYERS SDK is pinned to 3.1.16 so a future package release cannot silently change websocket behavior on deployment.

# v9.6.5 — invalidate stale VWAP setup on opposite close

- Fixed a strategy-state bug where an armed BUY CE/BUY PE setup could remain
  active after a later completed candle had closed through VWAP in the opposite
  direction.
- Opposite-side closed candles now invalidate the stale setup immediately and
  arm the new direction from that candle's close.
- This check happens before confirmation, so a wick cannot confirm the old
  direction when the candle ultimately closes on the opposite side of VWAP.
- The normal confirmation distance/window is unchanged.
- This keeps replay/backtest markers and the engine state aligned and prevents
  the stale setup from blocking subsequent valid entries.

## v9.6 — VWAP close-cross + gap-replay entry fix

- Genuine completed-candle VWAP side changes (`CLOSE_CROSS`) now always arm the normal confirmation state; chop/range filters no longer suppress them.
- The existing 15-point confirmation distance and 8-candle confirmation window are unchanged.
- REST live-gap repair now replays each recovered completed candle through the strategy state machine in chronological order, so crosses/confirmations that happened during a websocket outage can still trigger.
- Live websocket strategy transitions and gap-repair replays share a strategy lock to prevent state races.
- Live and repaired signals use one common execution/ledger path.

# Patch notes — live feed / candle gaps / VWAP

This build keeps a single FYERS market-data socket and lets fyers-apiv3 own
transport reconnects. The watchdog no longer closes the SDK socket or creates a
replacement while the SDK is reconnecting.

When a reconnect occurs, the engine asynchronously fetches the current trading
day from FYERS History and restores completed candles missed between the last
known candle and the current live candle. The REST repair runs outside the
websocket callback.

Also included:
- Streamlit hot-reload retry for `cloud_data` import.
- Thread-safe history merges during live-gap repair.
- VWAP payload fallback fills missing/invalid VWAP values from OHLC4 × volume,
  so the VWAP series remains visible when the source frame has an empty VWAP
  column.
- Existing Data-tab Complete Day behavior is retained: remove out-of-session
  rows and refill the canonical 09:15–15:30 IST session.


## v9.6.1 — bootstrap catch-up execution

- When asynchronous history loading finishes after the WebSocket has already advanced, the engine now immediately runs the same REST gap-repair/replay path. This covers slow initial data fetches, not only reconnects.
- Recovered completed candles are replayed chronologically through the live strategy and common execution path, so a missed VWAP confirmation can queue the corresponding BUY CE/BUY PE entry.
- Added signal-key de-duplication so a signal observed by both live ticks and REST replay cannot generate two entries.
- The chart may still contain a `HIST` visualization for the same strategy setup, but once the live execution ledger records the entry, the execution marker takes precedence on the live chart.


## v9.6.2 — market-session socket retry guard
- Market-data watchdog now treats **09:15–15:30 IST** as the exact regular session.
- After 15:30, the engine no longer raises stale-feed errors or repeatedly recreates the FYERS data socket.
- If `connect()` unexpectedly returns during market hours, the outer fallback reconnect now uses exponential backoff (5s → 60s) instead of a 2-second retry storm.
- REST gap repair is still scheduled whenever a transport interruption is detected, so missed candles can be recovered without blocking the WebSocket callback.
## v9.6.3 — PE replay exits + clearer trade labels

- Fixed replay/backtest SL/target direction for bearish signals: BUY PE is
  represented as a short underlying proxy, so a NIFTY fall is favorable
  instead of incorrectly triggering the PE stop loss.
- Kept live execution semantics unchanged: the actual option order remains
  BUY CE / BUY PE and its protection is based on the selected option premium.
- Reworked replay exit markers: `TARGET HIT • BUY CE/PE` is teal, while
  `STOP LOSS HIT • BUY CE/PE` uses a subtle red-orange tone.
- Manual/end-of-replay exits now use a neutral `EXIT • BUY CE/PE` label.
- No trailing stop was added; the configured fixed SL/target behavior remains
  unchanged.

## v9.6.3 replay-direction correction
- Corrected the earlier replay patch: a bearish VWAP signal is a **BUY PE**, but the replay underlying proxy moves in the opposite direction.
- BUY PE now has **SL above entry / target below entry** on the NIFTY proxy. BUY CE remains **SL below / target above**.
- Replay exit detection now checks the appropriate high/low for CE vs PE and calculates P&L with the same direction.
- This prevents a PE move from 24108.70 down to 24088.70 from being incorrectly reported as `STOP LOSS HIT`; that move is the PE target.
- Kept the actual live option protection path long-only: BUY CE and BUY PE both use option-premium SL below entry and target above entry.
- Chart exit labels now prioritize exit reason: TARGET is teal; STOP LOSS is red-orange, while BUY CE/BUY PE entry labels retain their existing style.
