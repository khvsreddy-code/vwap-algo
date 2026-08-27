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
3. The app requests FYERS' maximum practical option-chain window (strikecount=50) for the nearest expiry.
4. Every CE/PE contract returned by that chain is registered in Supabase; the live universe is cumulative as NIFTY moves.
5. The FYERS SymbolUpdate websocket subscribes to newly discovered contracts without deleting historical cloud rows.
6. Every tick is routed by its exact FYERS symbol.
7. The recorder builds independent 1-minute candles in memory.
8. Completed candles are upserted to Supabase in batches.
9. Option-chain snapshots refresh OI / OI change once per minute.
10. A background historical backfill fills NIFTY's missing 1-minute candles after startup; option history is intentionally not bulk-backfilled.
11. Temporary Supabase failures are retried; failed batches remain queued instead of being silently discarded.

If `CLOUD_DATA_REQUIRED=1` and the database cannot be health-checked, live startup is blocked with a clear error. The app will never claim that data is being saved when the cloud database is unavailable.

## 4. What is stored

For NIFTY:

- timestamp
- OHLC
- volume
- exact symbol

For each of maximum practical FYERS option-chain coverage:

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

## Wide ATM-following option universe

The cloud recorder follows the live NIFTY 50 spot price and uses the maximum practical FYERS option-chain window (`strikecount=50`). It records the CE/PE contracts returned by FYERS instead of hard-coding a 20+20 cloud universe.

When NIFTY moves far enough that the current chain window should be refreshed, the recorder fetches a new chain off the websocket callback thread, registers newly discovered contracts, subscribes them, and keeps previously discovered symbols/history in Supabase. The application can later select the nearest 20 CE + 20 PE from this wider historical universe.

## End-of-day gap recovery

The `☁️ Data` page has **Complete day & save ONLY missing data**. It fetches 1-minute FYERS history for the selected trading date for NIFTY and all option contracts known to the cloud registry, then checks the `(symbol, candle_start)` primary key before inserting. Existing rows are skipped and are not rewritten.

For option history, when FYERS supplies OI, the recovery derives `prev_oi` and minute-to-minute `oi_change` from the returned OI series. The live Option Chain snapshots remain the authoritative source during market hours.

The page also provides **Download ONLY newly recovered rows** so you can export exactly the rows that were missing at the time of recovery, plus the existing full-day CSV/ZIP download from Supabase.
