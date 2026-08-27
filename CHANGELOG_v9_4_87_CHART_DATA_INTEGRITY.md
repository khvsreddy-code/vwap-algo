# v9.4.87 — Chart data integrity

- Force a full chart bootstrap after top-level page switches so remounted charts cannot receive only a delta.
- Merge live NIFTY candles by exact timestamp instead of dataframe position.
- Seed in-progress NIFTY candles from matching REST history on connect/reconnect.
- Normalize chart timestamps to the selected IST timeframe bucket and merge duplicate buckets before transport.
- Enforce OHLC envelope invariants at the chart boundary.
- Keep the existing live marker/label renderer and incremental update path.
