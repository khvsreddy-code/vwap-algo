# V9.4.33 — CE/PE execution marker + local execution ledger fix

- Every successful live/test/paper option execution is recorded in an in-session execution ledger.
- Live chart markers use the exact timeframe candle bucket containing the trigger, so intrabar executions no longer disappear.
- BUY CE is shown below the candle with an up arrow; BUY PE is shown above the candle with a down arrow.
- Replay markers now use the same CE/PE placement rule.
- The terminal/charts Portfolio views show the local Algo Execution Ledger immediately, independently of delayed broker portfolio snapshots.
- Lightweight Charts marker rendering is compatible with both `setMarkers()` and the newer marker plugin API.
- All execution timestamps are displayed in IST.
