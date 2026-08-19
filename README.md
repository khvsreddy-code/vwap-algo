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
