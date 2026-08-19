# Auth persistence fix

This build fixes the FYERS OAuth flow in Streamlit:

- The FYERS login anchor explicitly uses `target="_self"` so the app does not intentionally open a new tab.
- App ID, Secret ID and Redirect URI are stored in a short-lived server-side auth-flow record keyed by a random `state`.
- When FYERS redirects back, the app restores those values even if Streamlit created a fresh session.
- The one-time auth code is exchanged for the access token automatically.
- `Back to Terminal` preserves the generated token and schedules an automatic FYERS connection.
- The manual `Get today's token` path remains as a fallback.
- Auth-flow records expire after 30 minutes and are consumed after callback.
