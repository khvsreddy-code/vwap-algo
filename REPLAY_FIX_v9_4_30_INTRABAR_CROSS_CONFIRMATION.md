# v9.4.30 - Intrabar VWAP Cross + Confirmation Fix

- Replay/backtest now recognizes a VWAP cross when the candle opens on one side of VWAP and closes on the other side, including the first candle of a trading session.
- If that same candle then reaches the configured confirmation distance, the entry is taken instead of being discarded.
- The live engine now evaluates the currently forming NIFTY candle on every tick after updating its OHLC, so the same intrabar sequence can trigger in real time.
- Existing pending setups are also checked against the current candle high/low on every live tick.
- The existing 09:15-15:15 IST new-entry window remains enforced.
- Pending VWAP setups do not carry across trading sessions.
