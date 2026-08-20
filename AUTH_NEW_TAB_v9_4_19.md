

## v9.4.19 — Open Auth Web in a new tab

- `Open Auth Web` now uses `target="_blank"` so the FYERS authorization flow opens in a separate browser tab.
- The original Streamlit tab remains on the app.
- FYERS still redirects to the registered bare Streamlit redirect URI (`https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/`) and the existing auth-code callback handler captures `?s=ok&code=200&auth_code=...&state=...`.
- If the FYERS browser session is already authenticated, FYERS can skip its login form; otherwise the user can log in in the new tab and then FYERS redirects back to the app.
