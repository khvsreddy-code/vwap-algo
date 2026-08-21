# Replay / chart fix v9.4.21

The v9.4.20 replay/full-window behavior is retained, with an additional hardening
layer for the Streamlit V2 chart component.

## Fixed
- Prevents malformed/NaN OHLC or marker values from reaching Lightweight Charts.
- Keeps the same chart component instance while replay advances.
- Replay cursor N sends candles 1..N, not the last 90.
- Full backtest and historical charts continue to send the complete loaded dataframe.
- Adds a compatibility fallback when a Streamlit runtime rejects V2 component
  width/height mount arguments.
- The backtest still operates on the exact dataframe loaded by the `Load historical
  candles` button; it does not generate a second/random dataset.
