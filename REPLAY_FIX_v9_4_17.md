# v9.4.17 — Replay step-by-step fix

Replay behavior:
- Starts at candle 1, not candle 30.
- Reset replay returns to candle 1 and stops playback.
- Next candle advances exactly one candle and stops playback.
- Run replay is an actual autoplay control: candles are revealed one at a time every ~700 ms.
- Run replay changes to Pause replay while playing.
- Playback automatically stops at the final candle.
- The chart renders only candles at or before the current replay cursor.
- Replay entries are recalculated on the visible candles, so simulated entries appear only after the strategy reaches them.
- Replay never submits broker orders.
