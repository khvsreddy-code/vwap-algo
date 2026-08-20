v9.4.28 replay action + event marker fix

- Correct mapping: NIFTY BUY signal -> BUY CE; NIFTY SELL signal -> BUY PE.
- Exit is a SELL of the held CE/PE.
- Replay markers use BUY arrow-up below candle for both CE and PE entries.
- Exit markers use SELL arrow-down above candle.
- Removed the fixed 5-minute marker tolerance. Events on 10/15/30/60-minute charts now map to the nearest candle.
- Events outside the currently revealed historical window are not pinned to the first/last candle.
- Open entries remain visible in the event ledger and on the replay chart.
- Replay/backtest event table explicitly shows BUY CE / BUY PE.
