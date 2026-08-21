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
