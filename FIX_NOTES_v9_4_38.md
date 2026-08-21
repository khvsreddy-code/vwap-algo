# v9.4.38 — false-trigger guard, anti-blink chart, and persistent signal display

## Strategy
- Confirmation move is now **15 points**.
- Confirmation window is now **8 candles**.
- Added a defensive signal guard: BUY cannot trigger below the configured confirmation level and SELL cannot trigger above it.
- The crossing candle still only arms the setup; it can never confirm itself.
- Historical confirmation continues to use candle high/low to model intrabar price reaching the configured level. Therefore a cross close of 24244 with a 15-point requirement needs 24259 to be reached; a later candle whose high only reaches 24250 cannot produce a BUY trigger.

## Chart stability
- Removed the main 1-second flicker source: the browser chart no longer removes/recreates the LTP price line on every fragment rerun.
- Price lines are only rebuilt when their values actually change.
- Markers are only resent when marker data changes.
- 31-day candle and VWAP series are no longer resent with `setData()` every second when unchanged.
- A live candle update uses Lightweight Charts `update()` when only the last candle changed.
- Historical reloads still use `setData()` so marker timestamps and candle data stay synchronized.
- Persistent execution levels retain the latest 3 execution events instead of only 2, reducing the chance that the yellow cross/green trigger line disappears after subsequent entries.

## Historical/live marker behavior
- Historical BUY CE / BUY PE markers remain calculated from the configured 15-point/8-candle state machine.
- Markers remain attached to the exact confirmation candle.
- Live execution markers continue to come from the persistent execution ledger.
- Live markers override a duplicate historical marker on the same candle/type.
- Trigger events remain persisted even if option selection or broker execution subsequently fails.

## Validation
- Python compilation passes for strategy.py, engine.py, app.py, live_chart.py, fyers_client.py, and paper_trading.py.
- Synthetic strategy test confirms:
  - cross close 24244 + 15 = 24259;
  - a next candle high of only 24250 does NOT trigger;
  - a later candle reaching 24259 DOES trigger.
