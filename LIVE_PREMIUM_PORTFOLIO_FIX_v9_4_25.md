# v9.4.25 — live premium chart + portfolio refresh fix

## Fixed
- Premium/option execution chart now advances from every selected-option FYERS tick.
- The current option candle is updated intrabar and a new candle is created when the timeframe rolls.
- Live chart viewport now scrolls to the newest candle when a new live candle timestamp arrives.
- Existing historical option candles are merged without creating duplicate timestamps.
- After a real FYERS order event, the app immediately refreshes the portfolio snapshot so the new position/order can appear without manual refresh.
- Replay/backtest behavior remains unchanged: replay uses the full revealed candle window and fit-to-content.

## Important
The premium chart is based on the selected option's actual FYERS market-data ticks after the option is selected. Historical candles remain the source for the pre-entry portion.
