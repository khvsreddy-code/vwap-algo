# FYERS VWAP Trader V9 - Test Entry Monitor

This build is for testing the real entry decision/execution path without depositing
money or sending broker orders.

When `Enable LIVE orders` is OFF and `Test LIVE entry engine (NO broker order)` is ON:

VWAP cross close -> arm setup -> live +N/-N confirmation -> select CE/PE in premium
band -> calculate SL/target -> build the exact FYERS order payload -> log it locally.

No FYERS order-placement endpoint is called in test-live mode.

The UI includes:
- toast notifications when a setup arms and when an entry triggers
- persistent in-session Entry Engine Monitor
- Entry State / trigger level
- entry attempt counter
- test-live entry counter
- exact dry-run order payload

Run:
    streamlit run app.py

Keep LIVE orders OFF while testing.


## UI additions
- `?page=charts` is a dedicated full-screen Charts view.
- Terminal and Charts navigation is at the top of the app.
- The Charts view uses a one-second Streamlit fragment so only the chart area updates.
- FYERS redirects with `auth_code` are captured automatically. The URL is cleared and a simple page displays the temporary auth code in a copyable code box.
- On the Terminal auth panel, the auth-code field is prefilled from the callback when available.
