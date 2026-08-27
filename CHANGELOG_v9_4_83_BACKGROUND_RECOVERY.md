# v9.4.83 — Background EOD Recovery

## Fix: live UI no longer blocks during complete-day recovery

The `Complete day & save ONLY missing data` action no longer runs the 209-symbol
FYERS REST loop inside Streamlit's script runner.

- EOD recovery runs in a daemon background worker.
- The worker uses its own FYERS REST client and Supabase client.
- The worker never calls Streamlit UI APIs.
- Recovery progress is published as plain state and polled by a 1-second
  Streamlit fragment.
- Terminal/live-chart fragments remain free to rerun while recovery is active.
- A per-session recovery owner prevents accidental duplicate recovery jobs.
- Finished recovery rows are copied into Streamlit session state only from the
  UI thread.
- Existing additive "save only missing candles" semantics are preserved.
- Fixed the recovery instrument lookup to use `CloudMarketStore.fetch_instruments()`.
- A regression test covers the background-worker lifecycle and verifies the old
  synchronous spinner path is gone.

This change is specifically intended to prevent the whole Streamlit page/window
from appearing frozen while hundreds of historical symbols are being fetched.
