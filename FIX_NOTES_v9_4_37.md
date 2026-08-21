# V9.4.37 — persistent historical/live CE/PE markers, option chart persistence, and new defaults

## Chart/marker fixes
- Terminal NIFTY chart now renders the full loaded one-month candle window.
- Terminal chart now shows historical BUY CE / BUY PE confirmation markers using the same VWAP confirmation state machine as live/replay.
- Live execution markers remain persisted in the local execution ledger and override duplicate historical markers on the same candle/type.
- Trigger/cross levels are reconstructed from the persistent execution ledger so the yellow cross and green confirmation level do not disappear when the strategy clears its pending setup after an entry.
- Marker placement remains snapped to the exact candle containing the confirmation trigger.

## Option chart fixes
- Selected option contract is rehydrated from the persistent execution ledger after reconnects and page switches.
- The selected option is re-subscribed to the existing FYERS socket when possible.
- Option history is loaded for 31 calendar days instead of 3.
- The option chart now sends the full loaded history instead of only the last 90 candles.
- Entry premium remains visible as an option-chart price level when protection state is not available after a rerun/reconnect.
- Execution events persist expiry and protection levels so the UI can restore the selected contract context.

## Strategy defaults
- Confirmation move after VWAP cross: **10 points** (was 15).
- Option premium minimum: **₹170** (was ₹180).
- Option premium maximum: **₹210** (was ₹200).
- Preferred premium remains **₹190**.

## Important behavior
A historical BUY CE / BUY PE marker means the NIFTY VWAP state machine would have confirmed that entry on that historical candle. Historical option premium is not fabricated because the historical option contract/chain snapshot is not part of the NIFTY candle dataset. Live entries use the actual FYERS option selected at the trigger and its live premium feed.
