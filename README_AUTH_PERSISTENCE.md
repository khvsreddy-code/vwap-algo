# FYERS Auth persistence / callback fix (v9.3.4.8)

- The registered Redirect URI is the app's built-in Auth route:
  `https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/?page=auth`
- App ID, Secret ID and Redirect URI are stored in a short-lived server-side auth-flow record keyed by a random `state`.
- FYERS returns `auth_code` and `state` to the Auth route.
- The callback is handled without redirecting to another route, eliminating the Streamlit Cloud redirect loop.
- The one-time auth code is exchanged automatically for today's access token.
- The Auth page shows the temporary code for transparency/copying, but manual token exchange is not required.
- `Back to Terminal` stays in the same Streamlit session, preserving the generated token and credentials.
- The Terminal consumes the pending `do_connect` flag and connects automatically.
- The FYERS login anchor uses `target="_self"` so it does not intentionally open a new browser tab.
- Auth-flow records expire after 30 minutes and are consumed after callback.
