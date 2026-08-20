# Replay full-window fix v9.4.20

- Replay no longer truncates the revealed candles to the last 250 candles in `app.py`.
- The chart component no longer hard-caps every view at 90 candles.
- Live charts keep the 90-candle performance window by default.
- Replay, full backtest, and standalone historical charts explicitly request the complete loaded dataset.
- Replay/backtest/historical views request `fitContent()` so moving the replay slider to candle 1245 displays candles 1..1245 instead of leaving the chart viewport stuck around the initial ~90 candles.
- The replay cursor logic remains one-candle-per-Next and one-candle-per-autoplay tick.
