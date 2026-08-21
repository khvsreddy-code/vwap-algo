# v9.4.32 — IST Chart Labels + Next-Candle Confirmation

- The VWAP-cross candle must close before a setup is armed.
- The crossing candle can never trigger the confirmation itself.
- Confirmation is evaluated from the next candle onward, up to the configured confirmation-bar window.
- Replay and full backtest use completed-candle high/low to model intrabar confirmation.
- Live mode checks the already-armed setup on subsequent live ticks/candles.
- Lightweight Charts keeps real UTC epoch timestamps but formats visible time/crosshair labels in `Asia/Kolkata`, so the market open displays as 09:15 IST instead of 03:45.
- Session filtering remains controlled by the configured IST algo start/end times.
