# Cloud market-data recorder — Supabase setup

This build stores market data in Supabase/Postgres. It does **not** create a local SQLite database or write market-data CSV files.

## 1. Create the Supabase database

Open your Supabase project's SQL Editor and run `supabase_schema.sql` once.

The schema creates:

- `instruments` — exact FYERS contract identity
- `market_candles_1m` — 1-minute NIFTY + option OHLCV/OI data
- `(symbol, candle_start)` primary key — safe idempotent upserts

## 2. Add server-side secrets

For Streamlit Cloud, put these in the app's **Secrets** settings. Do not put the secret key in browser code or commit it to Git.

```toml
SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
SUPABASE_SECRET_KEY = "sb_secret_..."
CLOUD_DATA_REQUIRED = "1"
```

The app also accepts the legacy `SUPABASE_SERVICE_ROLE_KEY` variable, but `SUPABASE_SECRET_KEY` is preferred for new Supabase projects.

## 3. What happens when Connect & Go LIVE is pressed

1. Supabase connection is health-checked.
2. FYERS option chain is queried.
3. The app chooses the 20 nearest CE strikes and 20 nearest PE strikes for the nearest expiry.
4. NIFTY + all 40 option symbols are registered in Supabase.
5. The FYERS SymbolUpdate websocket subscribes to all 41 symbols.
6. Every tick is routed by its exact FYERS symbol.
7. The recorder builds independent 1-minute candles in memory.
8. Completed candles are upserted to Supabase in batches.
9. Option-chain snapshots refresh OI / OI change once per minute.
10. A background historical backfill fills today's missing 1-minute candles after startup.
11. Temporary Supabase failures are retried; failed batches remain queued instead of being silently discarded.

If `CLOUD_DATA_REQUIRED=1` and the database cannot be health-checked, live startup is blocked with a clear error. The app will never claim that data is being saved when the cloud database is unavailable.

## 4. What is stored

For NIFTY:

- timestamp
- OHLC
- volume
- exact symbol

For each of 20 CE + 20 PE:

- timestamp
- expiry
- strike
- CE/PE
- premium OHLC
- LTP
- volume
- OI
- OI change versus previous session
- previous-session OI where available
- OI snapshot timestamp
- source (`fyers_websocket` or `fyers_history_backfill`)

## 5. Data Center

The new `☁️ Data` page reads directly from Supabase and provides:

- cloud connection/row-count health
- recorder status
- tracked-option count
- rows written / pending writes
- daily data viewer
- CSV download
- ZIP download split into NIFTY / CE / PE

The application only creates the download bytes in memory when the user requests a download; it does not maintain a local market-data archive.
