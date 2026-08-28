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
