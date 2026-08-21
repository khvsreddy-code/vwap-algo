# v9.4.35 — trigger/marker/ledger fix

## Entry rule
1. A completed candle must close across VWAP.
2. That crossing candle never enters.
3. The next candle through the configured confirmation window is checked.
4. As soon as price reaches the confirmation level intrabar, the strategy triggers.
5. BUY signal maps to CE and is marked below the triggering candle; SELL signal maps to PE and is marked above it.
6. The configured algo session is interpreted in IST and new entries are blocked outside it.

## What was fixed
- A local trigger event is recorded before option selection/order placement.
- The same event is updated from TRIGGERED -> OPTION_SELECTED -> EXECUTED/TEST/PAPER or REJECTED/FAILED.
- Chart markers therefore remain visible even if option selection/broker handling fails after the strategy trigger.
- Event updates use a stable event_id, so the Streamlit ledger updates instead of creating/dropping duplicate rows.
- Reconnecting the engine rehydrates the in-session execution ledger.
- Marker timestamp tolerance is derived from the actual chart candle spacing instead of assuming 5 minutes.
- Replay continues to use the same crossing-candle-then-confirmation-candle rule.
- All displayed event timestamps are IST.
