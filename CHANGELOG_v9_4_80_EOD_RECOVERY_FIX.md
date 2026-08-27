# v9.4.80 — EOD Recovery Fix

## Fixed
- FYERS `no_data` responses are now treated as normal empty history, not recoverable errors.
- EOD recovery ignores option contracts whose expiry is already before the selected trading day.
- EOD recovery only considers CE/PE contracts belonging to the NIFTY underlying, while always retaining the NIFTY index row.
- Genuine FYERS/API errors are still surfaced separately.
- Existing Supabase `(symbol, candle_start)` rows remain protected by the additive missing-only recovery path.

## Result
The cumulative instrument registry can safely contain old option contracts without the end-of-day button generating hundreds of false history errors for them.
