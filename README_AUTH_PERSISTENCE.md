# Auth persistence + old callback style fix

This build fixes the FYERS OAuth flow in Streamlit:

- The FYERS login anchor explicitly uses `target="_self"` so the app does not intentionally open a new tab.
- App ID, Secret ID and Redirect URI are stored in a short-lived server-side auth-flow record keyed by a random `state`.
- When FYERS redirects back, the app restores those values even if Streamlit created a fresh session.
- The one-time auth code is exchanged for the access token automatically.
- `Back to Terminal` preserves the generated token and schedules an automatic FYERS connection.
- The manual `Get today's token` path remains as a fallback.
- Auth-flow records expire after 30 minutes and are consumed after callback.


## v9.4.13 old callback behavior

This build intentionally restores the callback behavior from the earlier working build:

- FYERS Redirect URI is the **bare Streamlit root**:
  `https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/`
- The generated login URL uses that exact root as `redirect_uri`.
- After authorization, FYERS returns directly to:
  `/?s=ok&code=200&auth_code=...&state=...`
- The app captures the auth code before normal page navigation runs.
- The app does **not** redirect the callback to `?page=auth`.
- The app does **not** clear/rewrite the callback query string until the user clicks `Back to Terminal`.
- App ID, Secret ID and Redirect URI are restored from the saved short-lived auth-flow record.
- The auth code is exchanged automatically; when successful, `Back to Terminal` reconnects using the generated token.
- If token exchange fails, the auth-code screen remains visible with the error so the one-time code can still be inspected.
