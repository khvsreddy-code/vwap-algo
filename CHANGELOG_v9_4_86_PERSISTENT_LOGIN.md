# v9.4.86 — Persistent login across browser refresh

- Persist the current FYERS access token server-side after a successful connection.
- Restore the token after a Streamlit browser refresh.
- Automatically reconnect once on a fresh Streamlit session, without creating duplicate sockets on normal reruns.
- Never put the access token into query parameters or browser URLs.
- A stale/expired FYERS token still requires the normal fresh-token/login flow.
