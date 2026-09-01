# v9.7.0 — FYERS canonical live-data architecture

## Market data / chart correctness
- FYERS Data WebSocket is the primary live market-data source.
- A single tick stream now feeds the strategy and canonical chart candle aggregators.
- NIFTY chart candles for 1/3/5/10/15/30/60 minute views are aggregated from the same live FYERS ticks.
- Chart updates no longer fabricate a candle by overlaying one LTP onto a stale REST candle.
- REST History is used for bootstrap/recovery and cached chart history, not as a competing live stream.
- 5-minute charts reuse the engine's existing FYERS history instead of issuing a second identical History bootstrap request.
- History bootstrap was reduced to 7 days for the strategy engine; session VWAP is calculated per trading day, so a month of REST calls was unnecessary for live startup.
- Cloud option-chain/backfill setup now waits briefly after startup so it cannot compete with the primary live socket and cause avoidable 429/rate-limit pressure.
- FYERS authentication callback marks the data socket connected immediately after authentication; the UI no longer waits for the first tick just to report the transport state.

## VWAP state / visual correctness
- Old execution-ledger Cross/Trigger levels are no longer drawn as if they were the current pending setup.
- Only the current strategy state's Cross/Trigger levels are displayed. This removes the misleading stale low trigger after a failed cross/reclaim.
- The existing strategy state machine still replaces a failed cross with the new side on the next closed candle.

## Labels
- Target exits are shown in teal as `TARGET HIT`.
- Stop-loss exits use a restrained red-orange tone as `STOP LOSS HIT`.
- Generic exits remain neutral.

## Login / smoothness
- Persistent FYERS session storage remains enabled for browser refreshes.
- Added the missing `stat` import so the saved-session file permission hardening works as intended.

## Scope / live trading
- CE/PE selection remains based on the NIFTY strategy direction and the configured premium range.
- Live option entries remain BUY-only; protection is still calculated from the selected option premium and passed through the live FYERS order path.
- The market-data refactor does not replace the broker order path.
