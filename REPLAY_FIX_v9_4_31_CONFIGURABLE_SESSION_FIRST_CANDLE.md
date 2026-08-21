# v9.4.31 — Configurable Algo Session + Timeframe-Anchored First Candle

- User-configurable **Algo start time** and **Algo end time** in IST.
- Defaults remain **09:15–15:15 IST**.
- The same session window controls live entry decisions, live intrabar confirmation, replay, and full backtest.
- Existing open positions can still be evaluated for SL/Target after the configured end time.
- Live intraday candle bucketing is anchored to the configured start time, so the first candle uses the full selected timeframe.
- The first candle of each trading session is evaluated from its own OHLC movement; previous-day close/VWAP does not create a phantom opening cross.
- Historical setup seeding is restricted to the configured session to prevent out-of-session setups leaking into live trading.

Default:
- Start: 09:15 IST
- End: 15:15 IST

Change these values in the Strategy sidebar before running live or replay.
