# v9.4.34 — Persistent execution markers + ledger fix

## Fixed
- CE/PE executions are persisted in Streamlit session state immediately when the
  engine emits an execution event.
- Live chart markers are built from the persistent local ledger plus current
  engine events, not only from the broker portfolio.
- Portfolio `Algo Execution Ledger` uses the same persistent execution source.
- Naive execution timestamps are treated as IST instead of UTC when mapping to
  candles, eliminating the 5h30m marker offset.
- Browser-side marker rendering snaps an execution marker to the nearest real
  candle as a final safeguard, so intrabar executions remain visible.
- Execution events include `trigger_price` in addition to option premium.
- Existing replay/full-backtest markers continue to use the same candle mapping.

## Marker rules
- BUY CE: green arrow below the triggering candle.
- BUY PE: red arrow above the triggering candle.
- Marker text includes the option type and entry premium.

## Scope
No broker order semantics were changed. The fix is for visibility, persistence,
and candle/marker synchronization.
