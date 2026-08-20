# v9.4.18 — Auth rerun-loop fix

This build removes the browser-side Streamlit components used by v9.4.17 for
auth-field mirroring. Those components called `setStateValue()` while rendering,
which could cause repeated Streamlit reruns/refreshes.

Auth persistence now relies on the server-side OAuth flow record:
1. Current App ID, Secret ID and bare Redirect URI are saved immediately when
   the auth URL is prepared.
2. FYERS receives only the normal v3 OAuth URL.
3. The callback state restores the saved credentials on a fresh Streamlit
   session.
4. The old callback URL `/?s=ok&code=200&auth_code=...&state=...` remains intact.
5. The auth code is exchanged once when the user clicks Back to Terminal.
6. The Open Auth Web button is a plain same-tab link, not a custom component.

Important: Streamlit still executes a normal script rerun after widget clicks;
that is normal Streamlit behavior. This fix removes the continuous/looping
refresh caused by the auth components. It does not disable legitimate one-run
updates after a button click.
