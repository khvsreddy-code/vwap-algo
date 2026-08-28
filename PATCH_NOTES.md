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
