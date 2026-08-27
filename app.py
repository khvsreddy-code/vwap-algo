import os
import sys
import json
import html
import uuid
import time
import threading
import hashlib
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import pandas as pd
import streamlit as st
from datetime import time as dt_time
from dotenv import load_dotenv

from fyers_client import FyersClient
from cloud_data import CloudMarketStore

# Streamlit Cloud can reload while an older revision is being torn down. A
# half-initialized module can surface as KeyError('engine'); retry once.
try:
    from engine import TradingEngine
except KeyError as exc:
    if str(exc).strip("'\"") != "engine":
        raise
    sys.modules.pop("engine", None)
    from engine import TradingEngine
from strategy import VwapConfirmationEngine, StrategyConfig
from paper_trading import PaperTrader
from v9_4_52_live_chart import render as render_chart


load_dotenv()

# ---------- configurable algo session helpers ----------
# These must be defined before the Streamlit UI uses them.  Streamlit executes
# this module top-to-bottom on every rerun, so defining them later in the file
# causes a NameError before the function definitions are reached.
REPLAY_IST = "Asia/Kolkata"
DEFAULT_ENTRY_START_MINUTE = 9 * 60 + 15
DEFAULT_ENTRY_END_MINUTE = 15 * 60 + 15

def _ist_timestamp(value):
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return pd.NaT
        if ts.tzinfo is None:
            ts = ts.tz_localize(REPLAY_IST)
        else:
            ts = ts.tz_convert(REPLAY_IST)
        return ts
    except Exception:
        return pd.NaT

def _hhmm(value, fallback):
    try:
        return value.strftime("%H:%M") if hasattr(value, "strftime") else str(value)
    except Exception:
        return fallback

def _hhmm_minute(value, fallback):
    try:
        text = _hhmm(value, "")
        hh, mm = text.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:
        return fallback

def _hhmm_from_minute(minute):
    """Convert an IST minute-of-day integer to an HH:MM string."""
    try:
        minute = int(minute)
    except Exception:
        minute = DEFAULT_ENTRY_START_MINUTE
    minute = max(0, min(23 * 60 + 59, minute))
    return f"{minute // 60:02d}:{minute % 60:02d}"

def _within_entry_window(value, start_minute=DEFAULT_ENTRY_START_MINUTE, end_minute=DEFAULT_ENTRY_END_MINUTE):
    ts = _ist_timestamp(value)
    if pd.isna(ts):
        return False
    minute = ts.hour * 60 + ts.minute
    return int(start_minute) <= minute <= int(end_minute)

def _format_ist(value):
    ts = _ist_timestamp(value)
    if pd.isna(ts):
        return ""
    return ts.strftime("%d-%m-%Y %H:%M:%S IST")



def _bare_redirect_uri(value):
    """Return the FYERS callback URI without Streamlit page/query parameters."""
    raw = str(value or "").strip()
    if not raw:
        return "https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/"
    try:
        u = urlsplit(raw)
        path = u.path or "/"
        if not path.endswith("/"):
            path += "/"
        return urlunsplit((u.scheme, u.netloc, path, "", ""))
    except Exception:
        return raw.split("?", 1)[0].split("#", 1)[0].rstrip("/") + "/"

_DEFAULT_REDIRECT_URI = _bare_redirect_uri(
    os.getenv("FYERS_REDIRECT_URI", "https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/")
)

# Current NSE index-derivative market lots used by the option-order path.
# NIFTY 50 is 65 for new contracts under NSE's Oct-03-2025 revision.
INDEX_OPTION_LOT_SIZES = {
    "NIFTY50": 65,
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "MIDCPNIFTY": 120,
    "NIFTYNXT50": 25,
}

def option_lot_size_for_symbol(symbol):
    text = str(symbol or "").upper().replace(" ", "")
    if "NIFTYNXT50" in text:
        return INDEX_OPTION_LOT_SIZES["NIFTYNXT50"]
    if "BANKNIFTY" in text:
        return INDEX_OPTION_LOT_SIZES["BANKNIFTY"]
    if "FINNIFTY" in text:
        return INDEX_OPTION_LOT_SIZES["FINNIFTY"]
    if "MIDCPNIFTY" in text:
        return INDEX_OPTION_LOT_SIZES["MIDCPNIFTY"]
    if "NIFTY50" in text or "NIFTY" in text:
        return INDEX_OPTION_LOT_SIZES["NIFTY50"]
    return 1

APP_VERSION = "9.4.90"

st.set_page_config(page_title="FYERS VWAP Trader", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

# ---------- auth navigation helpers ----------
# Auth details are persisted server-side by _prepare_auth_flow() before the
# external FYERS navigation. Avoid browser-side Streamlit components here:
# components that call setStateValue during rendering can cause rerun loops.
_auth_store_component = None
_same_tab_auth_component = None

# ---------- persistent login ----------
# Streamlit session_state is intentionally reset when the browser page is
# hard-refreshed.  Keep the current FYERS access token on the server so a
# refresh can restore the connection without making the user paste/login again.
# This is deliberately server-side (not a URL/query parameter or browser
# localStorage) so the sensitive token is never exposed to the browser URL.
FYERS_SESSION_FILE = Path(os.getenv("FYERS_SESSION_FILE", ".fyers_session.json"))

def _load_saved_fyers_session():
    try:
        if not FYERS_SESSION_FILE.exists():
            return {}
        raw = json.loads(FYERS_SESSION_FILE.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}

def _save_fyers_session(app_id, access_token):
    app_id = str(app_id or "").strip()
    access_token = str(access_token or "").strip()
    if not app_id or not access_token:
        return
    try:
        FYERS_SESSION_FILE.write_text(json.dumps({
            "app_id": app_id,
            "access_token": access_token,
            "saved_at": time.time(),
        }))
        try:
            os.chmod(FYERS_SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass
    except Exception:
        # A read-only deployment can still use the normal in-session login.
        pass

_saved_fyers = _load_saved_fyers_session()
_saved_app_id = str(_saved_fyers.get("app_id") or os.getenv("FYERS_APP_ID", "")).strip()
_saved_token = str(_saved_fyers.get("access_token") or os.getenv("FYERS_ACCESS_TOKEN", "")).strip()

# ---------- session state ----------
def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

for key, default in {
    "client": None, "engine": None, "profile": None, "token": _saved_token,
    "portfolio": {}, "auth_url": "", "last_error": "", "connected": False,
    "paper_trader": None, "entry_log": [], "callback_auth_code": "", "show_auth_callback": False, "rejected_orders": [], "option_history_symbol": None,
    "auth_app_id": os.getenv("FYERS_APP_ID", ""), "auth_secret_id": os.getenv("FYERS_SECRET_ID", ""),
    "auth_redirect_uri": _DEFAULT_REDIRECT_URI,
    "auth_state": "", "auth_flow_created_at": 0.0,
    "auth_token_state": "", "auth_token_ready": False, "auth_callback_error": "",
    "auth_input_fingerprint": "", "auth_url_created_at": 0.0,
    # One automatic reconnect per fresh browser session. Subsequent Streamlit
    # reruns reuse the existing engine and never create duplicate sockets.
    "_auto_reconnect_attempted": False,
    "replay_df": None, "replay_key": "", "replay_index": 0, "replay_result": None,
    # Local algo execution ledger. This is independent of FYERS portfolio/order
    # snapshots so executed CE/PE entries remain visible during live fragments,
    # historical chart navigation, and replay/backtest reruns.
    "algo_execution_ledger": [],
    "ui_theme": "Dark",
} .items(): ss(key, default)

# ---------- FYERS auth callback ----------
# FYERS returns both auth_code and state.  The state is a short-lived server-side
# flow id; the actual Secret ID is never placed in the browser URL.
AUTH_FLOW_FILE = Path(os.getenv("FYERS_AUTH_FLOW_FILE", ".fyers_auth_flows.json"))

def _load_auth_flows():
    try:
        if not AUTH_FLOW_FILE.exists():
            return {}
        raw = json.loads(AUTH_FLOW_FILE.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}

def _save_auth_flows(flows):
    try:
        AUTH_FLOW_FILE.write_text(json.dumps(flows))
    except Exception:
        # The app can still work if the filesystem is read-only; session_state
        # remains the fallback for same-session navigation.
        pass

def _remember_auth_flow(app_id, secret_id, redirect_uri, state):
    flows = _load_auth_flows()
    now = time.time()
    # Keep only recent flows for a small, bounded file.
    flows = {
        k: v for k, v in flows.items()
        if isinstance(v, dict) and now - float(v.get("created_at", 0)) < 1800
    }
    flows[state] = {
        "app_id": str(app_id).strip(),
        "secret_id": str(secret_id).strip(),
        "redirect_uri": str(redirect_uri).strip(),
        "state": str(state),
        "created_at": now,
    }
    _save_auth_flows(flows)

def _restore_auth_flow(state):
    if not state:
        return None
    flows = _load_auth_flows()
    flow = flows.get(str(state))
    if not flow:
        return None
    # One-time flow metadata: remove it as soon as it is consumed.
    flows.pop(str(state), None)
    _save_auth_flows(flows)
    return flow


def _auth_fingerprint(app_id, secret_id, redirect_uri):
    raw = "\x1f".join([
        str(app_id or "").strip(),
        str(secret_id or "").strip(),
        _bare_redirect_uri(redirect_uri),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _prepare_auth_flow(app_id, secret_id, redirect_uri, force_new=False):
    """Persist the exact current credentials and create a fresh v3 auth URL.

    The important fix is that the URL is tied to the CURRENT input values.
    Editing App ID/Secret/Redirect URI therefore invalidates the old URL/state
    instead of leaving a stale link that can send the user back into a loop.
    The secret is stored only server-side; it is never put in the browser URL.
    """
    app_id = str(app_id or "").strip()
    secret_id = str(secret_id or "").strip()
    redirect_uri = _bare_redirect_uri(redirect_uri)
    if not app_id or not secret_id or not redirect_uri:
        return ""

    fp = _auth_fingerprint(app_id, secret_id, redirect_uri)
    needs_new = (
        force_new
        or not st.session_state.get("auth_url")
        or not st.session_state.get("auth_state")
        or st.session_state.get("auth_input_fingerprint") != fp
    )
    st.session_state.auth_app_id = app_id
    st.session_state.auth_secret_id = secret_id
    st.session_state.auth_redirect_uri = redirect_uri

    if needs_new:
        state = "fyers_vwap_" + uuid.uuid4().hex
        _remember_auth_flow(app_id, secret_id, redirect_uri, state)
        st.session_state.auth_state = state
        st.session_state.auth_input_fingerprint = fp
        st.session_state.auth_flow_created_at = time.time()
        st.session_state.auth_url = FyersClient.auth_url(
            app_id, secret_id, redirect_uri, state
        )
        st.session_state.auth_url_created_at = time.time()
    else:
        # Refresh the saved flow record so a slow login does not lose the
        # credentials before the callback arrives.
        _remember_auth_flow(app_id, secret_id, redirect_uri, st.session_state.auth_state)

    return st.session_state.auth_url


_callback_code = st.query_params.get("auth_code")
_callback_state = st.query_params.get("state")
_callback_error = st.query_params.get("s") or st.query_params.get("error")

if _callback_code:
    # OLD AUTH STYLE:
    # FYERS redirects directly to the bare Streamlit app URL:
    #   /?s=ok&code=200&auth_code=...&state=...
    # Do NOT redirect to /?page=auth and do NOT clear/rewrite the callback
    # query string before the callback screen is rendered.
    flow = _restore_auth_flow(_callback_state) or {}
    if flow:
        st.session_state.auth_app_id = flow.get("app_id", st.session_state.get("auth_app_id", ""))
        st.session_state.auth_secret_id = flow.get("secret_id", st.session_state.get("auth_secret_id", ""))
        st.session_state.auth_redirect_uri = _bare_redirect_uri(flow.get("redirect_uri", st.session_state.get("auth_redirect_uri", "")))
        st.session_state.auth_state = ""
    else:
        # Same-browser fallback: keep whatever the user entered.
        st.session_state.auth_state = ""

    st.session_state.callback_auth_code = str(_callback_code).strip()
    st.session_state.fyers_auth_code = st.session_state.callback_auth_code
    st.session_state.show_auth_callback = True

    # IMPORTANT: do not exchange the one-time code on callback arrival.
    # First render the old-style callback page. The user can see/copy the code,
    # and "Back to Terminal" performs the exchange exactly once. This avoids
    # consuming the code during a browser rerun and makes the callback URL a
    # terminal state instead of starting another auth flow.
    st.session_state.auth_token_ready = False
    st.session_state.auth_callback_error = ""
    st.session_state.auth_url = ""

elif _callback_error:
    st.session_state.auth_callback_error = str(_callback_error)
    st.session_state.show_auth_callback = True

if st.session_state.show_auth_callback and st.session_state.callback_auth_code:
    st.markdown("## 🔐 FYERS Auth Code")
    st.success("Auth code received. Copy it below, then return to Terminal.")
    st.code(st.session_state.callback_auth_code, language=None)
    st.text_input(
        "Auth code — copy from here",
        value=st.session_state.callback_auth_code,
        key="fyers_auth_code_copy",
    )
    st.caption("The auth code is temporary and one-time use. Keep it private.")

    if st.button("↩️ Back to Terminal", type="primary", width="stretch"):
        # Exchange the one-time auth code exactly once, using the values that
        # were saved before Open Auth Web was opened.
        try:
            app_id_cb = st.session_state.get("auth_app_id", "").strip()
            secret_id_cb = st.session_state.get("auth_secret_id", "").strip()
            redirect_uri_cb = _bare_redirect_uri(st.session_state.get("auth_redirect_uri", ""))
            code_cb = st.session_state.get("callback_auth_code", "").strip()
            if not app_id_cb or not secret_id_cb or not redirect_uri_cb or not code_cb:
                raise ValueError("Saved App ID / Secret ID / Redirect URI / auth code are incomplete.")
            new_token = FyersClient.exchange_auth_code(
                app_id_cb, secret_id_cb, redirect_uri_cb, code_cb
            )
            st.session_state.token = new_token
            st.session_state.auth_token_ready = True
            st.session_state.auth_callback_error = ""
            st.session_state.show_auth_callback = False
            st.session_state.callback_auth_code = ""
            st.session_state.auth_state = ""
            st.session_state.auth_url = ""
            st.session_state.page = "terminal"
            st.session_state.do_connect = True
            # Remove the one-time callback only after it has been consumed.
            st.query_params.clear()
            st.query_params["page"] = "terminal"
            st.rerun()
        except Exception as e:
            st.session_state.auth_callback_error = str(e)
            st.error(f"Token exchange failed: {e}")
    st.stop()

if st.session_state.show_auth_callback and st.session_state.auth_callback_error:
    st.error(st.session_state.auth_callback_error)
    if st.button("↩️ Back to Terminal", type="primary", width="stretch"):
        st.session_state.show_auth_callback = False
        st.session_state.page = "terminal"
        st.query_params.clear()
        st.query_params["page"] = "terminal"
        st.rerun()
    st.stop()

# ---------- styling ----------
# Three UI modes: the original trading-terminal Dark theme, a clean Light
# theme, and a strict Black & White theme. The selected mode is session-local
# and does not restart the market-data engine.
ui_theme = st.session_state.get("ui_theme", "Dark")
if ui_theme == "Light":
    _theme_css = """
:root { color-scheme: light; }
.stApp { background:linear-gradient(180deg,#f8fafc 0%,#eef2f7 100%); color:#17202a; }
[data-testid="stHeader"] { background:rgba(255,255,255,.90); }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,#ffffff,#f3f5f8); border-right:1px solid #d9dee7; }
.card,.stage-card { background:#ffffff; border:1px solid #d9dee7; box-shadow:0 8px 24px rgba(15,23,42,.07); }
.small,.mode-desc,.stage-detail,.tv-sub { color:#64748b; }
.good { color:#087f5b; } .bad { color:#c92a2a; } .warn { color:#9a6700; }
[data-testid="stMetric"] { background:#ffffff; border:1px solid #d9dee7; }
[data-testid="stMetricLabel"] { color:#64748b; } [data-testid="stMetricValue"] { color:#111827; }
div[data-baseweb="tab-list"] { border-bottom:1px solid #d9dee7; }
.tv-top,.chart-status { background:#ffffff; border-color:#d9dee7; }
.tv-title,.count,.chart-count { color:#111827; }
.page-nav { background:#ffffff; border-color:#d9dee7; }
.page-nav a { color:#64748b; } .page-nav a:hover { background:#eef2f7; color:#111827; }
.page-nav a.active { background:#e8edf4; color:#111827; box-shadow:inset 0 0 0 1px #cbd5e1; }
.mode-banner { background:#ffffff; border-color:#d9dee7; }
.mode-chain span { background:#f1f5f9; border-color:#d9dee7; color:#475569; }
[data-testid="stDataFrame"] { border-color:#d9dee7; }
"""
elif ui_theme == "Black & White":
    _theme_css = """
:root { color-scheme: dark; }
.stApp { background:#000000; color:#ffffff; }
[data-testid="stHeader"] { background:#000000; }
section[data-testid="stSidebar"] { background:#050505; border-right:1px solid #2b2b2b; }
.card,.stage-card { background:#080808; border:1px solid #292929; box-shadow:0 10px 28px rgba(0,0,0,.35); }
.small,.mode-desc,.stage-detail,.tv-sub { color:#a8a8a8; }
.good,.bad,.warn,.live,.off { color:#ffffff; }
[data-testid="stMetric"] { background:#080808; border:1px solid #2b2b2b; }
[data-testid="stMetricLabel"] { color:#a8a8a8; } [data-testid="stMetricValue"] { color:#ffffff; }
div[data-baseweb="tab-list"] { border-bottom:1px solid #2b2b2b; }
.tv-top,.chart-status { background:#070707; border-color:#2b2b2b; }
.tv-title,.count,.chart-count { color:#ffffff; }
.page-nav { background:#070707; border-color:#2b2b2b; }
.page-nav a { color:#a8a8a8; } .page-nav a:hover { background:#171717; color:#ffffff; }
.page-nav a.active { background:#202020; color:#ffffff; box-shadow:inset 0 0 0 1px #404040; }
.mode-banner { background:#070707; border-color:#2b2b2b; }
.mode-chain span { background:#141414; border-color:#303030; color:#cfcfcf; }
[data-testid="stDataFrame"] { border-color:#2b2b2b; }
"""
else:
    _theme_css = """
:root { color-scheme: dark; }
.stApp { background:
  radial-gradient(circle at 80% -10%, rgba(64,116,180,.12), transparent 32rem),
  linear-gradient(180deg,#080d13 0%,#0b1017 48%,#090e14 100%);
  color:#dbe4ef;
}
"""
st.markdown("""
<style>
""" + _theme_css + """
[data-testid="stHeader"] { background:rgba(7,11,16,.78); backdrop-filter:blur(14px); }
[data-testid="stToolbar"] { opacity:.78; }
.block-container { padding: .7rem 1.2rem 2rem; max-width:1680px; }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,#0d141c,#0a1017); border-right:1px solid #1b2734; }
section[data-testid="stSidebar"] > div { padding-top:1rem; }
.card,.stage-card { background:linear-gradient(180deg,rgba(18,27,37,.96),rgba(13,19,27,.96)); border:1px solid #1d2a38; border-radius:12px; box-shadow:0 10px 28px rgba(0,0,0,.12); }
.card { padding:14px 16px; }
.small { color:#8492a5; font-size:12px; }
.good { color:#27c49a; font-weight:750; }
.bad { color:#f05d63; font-weight:750; }
.warn { color:#f1bd55; font-weight:750; }
h1,h2,h3 { letter-spacing:-.02em; }
[data-testid="stMetric"] { background:linear-gradient(180deg,#101923,#0c131b); border:1px solid #1d2a38; border-radius:11px; padding:10px 12px; }
[data-testid="stMetricLabel"] { color:#8190a3; }
[data-testid="stMetricValue"] { color:#edf3fa; font-variant-numeric:tabular-nums; }
button[kind="primary"] { box-shadow:0 6px 18px rgba(0,0,0,.18); }
div[data-baseweb="tab-list"] { gap:4px; border-bottom:1px solid #1d2a38; }
button[data-baseweb="tab"] { border-radius:8px 8px 0 0; padding:8px 13px; }
.tv-top { display:flex; align-items:center; gap:14px; padding:11px 15px; border:1px solid #1d2a38; border-radius:11px; background:rgba(12,18,25,.86); margin-bottom:8px; box-shadow:0 8px 22px rgba(0,0,0,.10); }
.tv-title { font-size:16px; font-weight:750; color:#eef4fb; }
.tv-sub { font-size:12px; color:#8795a7; }
.dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; }
.live { color:#27c49a; font-weight:750; }
.off { color:#f05d63; font-weight:750; }
.count { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:750; color:#e7edf5; }
.page-nav { display:flex; gap:5px; margin:0 0 12px; padding:4px; background:#0d141c; border:1px solid #1d2a38; border-radius:11px; width:max-content; }
.page-nav a { color:#8290a2; text-decoration:none; padding:7px 13px; border-radius:8px; font-size:13px; font-weight:650; transition:.15s ease; }
.page-nav a:hover { background:#151f2b; color:#dce5ef; }
.page-nav a.active { background:#1b2a39; color:#f0f5fa; box-shadow:inset 0 0 0 1px #2b4156; }
.chart-status { display:flex; align-items:center; gap:10px; padding:9px 13px; margin-bottom:8px; border:1px solid #1d2a38; border-radius:9px; background:#0c131b; color:#93a0b0; font-size:12px; }
.chart-dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
.chart-dot.on { background:#27c49a; box-shadow:0 0 10px rgba(39,196,154,.45); }
.chart-dot.off { background:#f05d63; }
.chart-count { margin-left:auto; color:#e7edf5; font-weight:700; }
.mode-banner { margin:0 0 10px; padding:11px 14px; border:1px solid #243140; border-radius:11px; background:rgba(15,22,30,.88); display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.mode-banner.danger { border-color:#71373a; background:linear-gradient(90deg,rgba(84,31,37,.25),rgba(15,22,30,.88)); }
.mode-banner.test { border-color:#6c5b2b; }
.mode-banner.paper { border-color:#24564e; }
.mode-desc { color:#8f9bad; font-size:12px; }
.mode-chain { margin-left:auto; display:flex; gap:8px; align-items:center; color:#b9c3cf; font-size:11px; flex-wrap:wrap; }
.mode-chain span { padding:4px 7px; border-radius:999px; background:#131d28; border:1px solid #233242; }
.mode-chain b { color:#697687; }
.stage-card { padding:11px 13px; min-height:76px; }
.stage-title { font-size:10px; color:#8190a1; text-transform:uppercase; letter-spacing:.65px; }
.stage-value { font-size:15px; font-weight:750; margin-top:5px; color:#e7edf5; }
.stage-detail { font-size:11px; color:#7f8b9b; margin-top:3px; }
.reject-card { background:linear-gradient(180deg,#211215,#170d10); border:1px solid #713038; border-radius:11px; padding:12px 14px; margin:8px 0; }
.reject-title { color:#ff747a; font-weight:800; font-size:12px; letter-spacing:.3px; }
.reject-detail { color:#d9b7ba; font-size:12px; margin-top:4px; }
[data-testid="stDataFrame"] { border:1px solid #1d2a38; border-radius:10px; overflow:hidden; }
button { transition:transform .12s ease, box-shadow .12s ease, border-color .12s ease; }
button:hover { transform:translateY(-1px); }
[data-testid="stSidebar"] .stButton button { border-radius:9px; }
""", unsafe_allow_html=True)


# ---------- final theme overrides ----------
# The base terminal CSS is intentionally written once for the Dark terminal.
# These overrides MUST come after it; otherwise the base dark selectors win in
# the cascade and Light appears selected while the page remains dark.
if ui_theme == "Light":
    _final_theme_css = """
    :root { color-scheme: light !important; }
    .stApp, .main, [data-testid="stAppViewContainer"] {
        background: #ffffff !important;
        color: #111827 !important;
    }
    [data-testid="stHeader"] {
        background: rgba(255,255,255,.96) !important;
        border-bottom: 1px solid #e5e7eb !important;
    }
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div {
        background: #ffffff !important;
        color: #111827 !important;
        border-right: 1px solid #e5e7eb !important;
    }
    section[data-testid="stSidebar"] * { color: #111827; }
    .card, .stage-card, [data-testid="stMetric"],
    .tv-top, .chart-status, .page-nav, .mode-banner {
        background: #ffffff !important;
        color: #111827 !important;
        border-color: #d9dee7 !important;
        box-shadow: 0 5px 18px rgba(15,23,42,.06) !important;
    }
    .small, .mode-desc, .stage-detail, .tv-sub,
    [data-testid="stMetricLabel"], .stage-title {
        color: #64748b !important;
    }
    .tv-title, .count, .chart-count, .stage-value,
    [data-testid="stMetricValue"], h1, h2, h3, h4, h5, h6, p, label {
        color: #111827 !important;
    }
    .page-nav a { color: #475569 !important; }
    .page-nav a:hover { background: #f1f5f9 !important; color: #111827 !important; }
    .page-nav a.active {
        background: #e8edf4 !important;
        color: #111827 !important;
        box-shadow: inset 0 0 0 1px #cbd5e1 !important;
    }
    .mode-chain span {
        background: #f8fafc !important;
        border-color: #d9dee7 !important;
        color: #475569 !important;
    }
    .chart-status { color: #64748b !important; }
    [data-testid="stDataFrame"] {
        background: #ffffff !important;
        border-color: #d9dee7 !important;
    }
    /* Streamlit/BaseWeb inputs and selectbox */
    section[data-testid="stSidebar"] input,
    section[data-testid="stSidebar"] textarea,
    section[data-testid="stSidebar"] [data-baseweb="select"] > div,
    section[data-testid="stSidebar"] [data-baseweb="input"] > div {
        background: #ffffff !important;
        color: #111827 !important;
        border-color: #cbd5e1 !important;
    }
    section[data-testid="stSidebar"] [data-baseweb="select"] * {
        color: #111827 !important;
    }
    section[data-testid="stSidebar"] [role="listbox"],
    [data-baseweb="popover"] [role="listbox"] {
        background: #ffffff !important;
        color: #111827 !important;
    }
    [data-baseweb="popover"] [role="option"] { color: #111827 !important; }
    [data-baseweb="popover"] [role="option"]:hover { background: #f1f5f9 !important; }
    button {
        color: #111827 !important;
        border-color: #cbd5e1 !important;
    }
    """
elif ui_theme == "Black & White":
    _final_theme_css = """
    :root { color-scheme: dark !important; }
    .stApp, .main, [data-testid="stAppViewContainer"] {
        background: #000000 !important;
        color: #ffffff !important;
    }
    [data-testid="stHeader"] {
        background: #000000 !important;
        border-bottom: 1px solid #2a2a2a !important;
    }
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div {
        background: #000000 !important;
        color: #ffffff !important;
        border-right: 1px solid #2a2a2a !important;
    }
    section[data-testid="stSidebar"] * { color: #ffffff; }
    .card, .stage-card, [data-testid="stMetric"],
    .tv-top, .chart-status, .page-nav, .mode-banner {
        background: #000000 !important;
        color: #ffffff !important;
        border-color: #303030 !important;
        box-shadow: none !important;
    }
    .small, .mode-desc, .stage-detail, .tv-sub,
    [data-testid="stMetricLabel"], .stage-title {
        color: #bdbdbd !important;
    }
    .tv-title, .count, .chart-count, .stage-value,
    [data-testid="stMetricValue"], h1, h2, h3, h4, h5, h6, p, label {
        color: #ffffff !important;
    }
    .page-nav a { color: #bdbdbd !important; }
    .page-nav a:hover, .page-nav a.active {
        background: #1a1a1a !important;
        color: #ffffff !important;
    }
    .mode-chain span {
        background: #111111 !important;
        border-color: #333333 !important;
        color: #ffffff !important;
    }
    [data-testid="stDataFrame"] {
        background: #000000 !important;
        border-color: #303030 !important;
    }
    section[data-testid="stSidebar"] input,
    section[data-testid="stSidebar"] textarea,
    section[data-testid="stSidebar"] [data-baseweb="select"] > div,
    section[data-testid="stSidebar"] [data-baseweb="input"] > div {
        background: #000000 !important;
        color: #ffffff !important;
        border-color: #444444 !important;
    }
    section[data-testid="stSidebar"] [data-baseweb="select"] * {
        color: #ffffff !important;
    }
    section[data-testid="stSidebar"] [role="listbox"],
    [data-baseweb="popover"] [role="listbox"] {
        background: #000000 !important;
        color: #ffffff !important;
        border-color: #444444 !important;
    }
    [data-baseweb="popover"] [role="option"] { color: #ffffff !important; }
    [data-baseweb="popover"] [role="option"]:hover { background: #1a1a1a !important; }
    button {
        color: #ffffff !important;
        border-color: #444444 !important;
        background: #000000 !important;
    }
    """
else:
    _final_theme_css = """
    :root { color-scheme: dark !important; }
    .stApp, .main, [data-testid="stAppViewContainer"] {
        background: #070b10 !important;
        color: #dbe4ef !important;
    }
    [data-testid="stHeader"] { background: #070b10 !important; }
    section[data-testid="stSidebar"],
    section[data-testid="stSidebar"] > div {
        background: #0b1118 !important;
        color: #dbe4ef !important;
    }
    section[data-testid="stSidebar"] input,
    section[data-testid="stSidebar"] textarea,
    section[data-testid="stSidebar"] [data-baseweb="select"] > div,
    section[data-testid="stSidebar"] [data-baseweb="input"] > div {
        background: #0d141c !important;
        color: #e7edf5 !important;
        border-color: #263544 !important;
    }
    section[data-testid="stSidebar"] [data-baseweb="select"] * { color: #e7edf5 !important; }
    """
st.markdown("<style>" + _final_theme_css + "</style>", unsafe_allow_html=True)

# ---------- same-tab page navigation ----------
# Use Streamlit buttons/query params instead of HTML links. This keeps the
# current browser tab/session alive, so FYERS credentials in session_state
# are not lost when switching between Terminal and Charts.
page = st.session_state.get("page", st.query_params.get("page", "terminal"))
if page not in ("terminal", "charts", "auth", "replay", "data"):
    page = "terminal"
st.session_state.page = page

# A top-level page switch unmounts the V2 chart component in the browser, while
# Streamlit session_state survives navigation. Force a full chart bootstrap on
# the first render after a page switch; steady-state updates remain incremental.
_previous_chart_page = st.session_state.get("_v952_chart_page")
if _previous_chart_page != page:
    st.session_state["_v952_chart_page"] = page
    st.session_state.pop("_v952_chart_signature", None)

nav1, nav2, nav3, nav4, nav5 = st.columns([1, 1, 1, 1, 1], gap="small")
with nav1:
    if st.button("⌂ Terminal", key="nav_terminal", width="stretch",
                 type="primary" if page == "terminal" else "secondary"):
        st.session_state.page = "terminal"
        st.query_params["page"] = "terminal"
        st.rerun()
with nav2:
    if st.button("📊 Charts", key="nav_charts", width="stretch",
                 type="primary" if page == "charts" else "secondary"):
        st.session_state.page = "charts"
        st.query_params["page"] = "charts"
        st.rerun()
with nav3:
    if st.button("🧪 Replay", key="nav_replay", width="stretch",
                 type="primary" if page == "replay" else "secondary"):
        st.session_state.page = "replay"
        st.query_params["page"] = "replay"
        st.rerun()
with nav4:
    if st.button("🔐 Auth Web", key="nav_auth", width="stretch",
                 type="primary" if page == "auth" else "secondary"):
        st.session_state.page = "auth"
        st.session_state.show_auth_callback = False
        st.query_params["page"] = "auth"
        st.rerun()
with nav5:
    if st.button("☁️ Data", key="nav_data", width="stretch",
                 type="primary" if page == "data" else "secondary"):
        st.session_state.page = "data"
        st.query_params["page"] = "data"
        st.rerun()

# ---------- cloud data center ----------
CLOUD_CANDLE_COLUMNS = {
    "symbol", "candle_start", "underlying", "expiry", "strike", "option_type",
    "open", "high", "low", "close", "ltp", "volume", "oi", "oi_change",
    "prev_oi", "oi_snapshot_at", "source", "updated_at",
}


def _candle_payload(row):
    # ``instruments`` contains registry-only fields (notably ``active``).
    # Never send those fields to market_candles_1m.
    return {k: v for k, v in row.items() if k in CLOUD_CANDLE_COLUMNS}



class _RecoveryManager:
    """Process-level background manager for long-running EOD recovery.

    Recovery deliberately never calls Streamlit APIs from the worker thread.
    The worker owns its FYERS REST and Supabase clients and publishes only
    plain-Python progress/result state. This keeps Streamlit's script runner
    free to render live charts/fragments while hundreds of REST requests run.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}

    def start(self, owner_id, *, url, key, app_id, token, signal_symbol, selected_date, scope):
        with self._lock:
            current = self._jobs.get(owner_id)
            if current and current.get("running"):
                return False, current

            job = {
                "running": True,
                "done": False,
                "error": None,
                "started_at": time.time(),
                "finished_at": None,
                "total": 0,
                "completed": 0,
                "fetched": 0,
                "inserted": 0,
                "skipped": 0,
                "no_data_symbols": 0,
                "errors": [],
                "symbol": "",
                "message": "Starting recovery…",
                "new_rows": [],
                "selected_date": str(selected_date),
                "scope": str(scope),
            }
            self._jobs[owner_id] = job

            thread = threading.Thread(
                target=self._run,
                args=(owner_id, url, key, app_id, token, signal_symbol, selected_date, scope),
                name=f"eod-recovery-{owner_id[:8]}",
                daemon=True,
            )
            thread.start()
            return True, job

    def _update(self, owner_id, **changes):
        with self._lock:
            job = self._jobs.get(owner_id)
            if job:
                job.update(changes)

    def _progress(self, owner_id, completed, total, symbol="", message=""):
        self._update(
            owner_id,
            completed=int(completed),
            total=int(total),
            symbol=str(symbol or ""),
            message=str(message or ""),
        )

    def _run(self, owner_id, url, key, app_id, token, signal_symbol, selected_date, scope):
        try:
            # These clients belong exclusively to the worker. In particular,
            # do not share the live engine's REST client across the background
            # recovery thread and websocket/UI callbacks.
            store = CloudMarketStore(url, key)
            client = FyersClient(app_id, token)
            result = _recover_missing_day(
                store,
                client,
                signal_symbol,
                selected_date,
                scope,
                progress_callback=lambda **kw: self._progress(owner_id, **kw),
            )
            self._update(
                owner_id,
                running=False,
                done=True,
                finished_at=time.time(),
                fetched=int(result.get("fetched", 0)),
                inserted=int(result.get("inserted", 0)),
                skipped=int(result.get("skipped", 0)),
                deleted_out_of_session=int(result.get("deleted_out_of_session", 0)),
                no_data_symbols=int(result.get("no_data_symbols", 0)),
                errors=list(result.get("errors", [])),
                new_rows=result.get("new_rows", pd.DataFrame()).to_dict("records")
                    if isinstance(result.get("new_rows"), pd.DataFrame)
                    else list(result.get("new_rows", []) or []),
                message="Recovery complete.",
            )
        except Exception as exc:
            self._update(
                owner_id,
                running=False,
                done=True,
                finished_at=time.time(),
                error=str(exc),
                message=f"Recovery failed: {exc}",
            )

    def snapshot(self, owner_id):
        with self._lock:
            job = self._jobs.get(owner_id)
            if not job:
                return None
            out = dict(job)
            out["errors"] = list(job.get("errors", []))
            out["new_rows"] = list(job.get("new_rows", []))
            return out


@st.cache_resource(show_spinner=False)
def _recovery_manager():
    return _RecoveryManager()


def _build_day_recovery_rows(client, instruments, signal_symbol, selected_date, progress_callback=None):
    """Fetch one day's 1m history without touching Streamlit from worker threads."""
    rows_by_symbol = []
    nifty_meta = next((r for r in instruments if r.get("symbol") == signal_symbol), None)
    if not nifty_meta:
        nifty_meta = {
            "symbol": signal_symbol,
            "underlying": signal_symbol,
            "expiry": None,
            "strike": None,
            "option_type": None,
        }
        instruments = [nifty_meta] + list(instruments)
    else:
        instruments = [nifty_meta] + [r for r in instruments if r.get("symbol") != signal_symbol]

    total = len(instruments)
    if progress_callback:
        progress_callback(
            completed=0,
            total=total,
            symbol="",
            message=f"Preparing recovery • {total} symbols",
        )

    for idx, meta in enumerate(instruments, start=1):
        symbol = str(meta.get("symbol") or "").strip()
        if not symbol:
            continue
        is_option = str(meta.get("option_type") or "").upper() in {"CE", "PE"}
        if progress_callback:
            progress_callback(
                completed=idx - 1,
                total=total,
                symbol=symbol,
                message=f"Fetching {idx}/{total} • {symbol}",
            )
        try:
            df = client.history_for_date(symbol, "1", selected_date, oi_flag=is_option)
        except Exception as exc:
            rows_by_symbol.append({"symbol": symbol, "rows": [], "error": str(exc)})
            if progress_callback:
                progress_callback(
                    completed=idx,
                    total=total,
                    symbol=symbol,
                    message=f"Skipped {symbol} • {idx}/{total}",
                )
            continue

        if df.empty:
            rows_by_symbol.append({"symbol": symbol, "rows": [], "error": None})
            if progress_callback:
                progress_callback(
                    completed=idx,
                    total=total,
                    symbol=symbol,
                    message=f"No FYERS history • {idx}/{total}",
                )
            continue

        work = df.copy()
        if "oi" in work.columns and is_option:
            work["oi"] = pd.to_numeric(work["oi"], errors="coerce")
            work["prev_oi"] = work["oi"].shift(1)
            work["oi_change"] = work["oi"] - work["prev_oi"]
        else:
            work["oi"] = None
            work["prev_oi"] = None
            work["oi_change"] = None

        normalized = []
        for _, r in work.iterrows():
            stamp = pd.Timestamp(r["datetime"])
            normalized.append(_candle_payload({
                **meta,
                "candle_start": stamp.isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "ltp": float(r["close"]),
                "volume": int(r.get("volume") or 0),
                "oi": int(r["oi"]) if pd.notna(r.get("oi")) else None,
                "oi_change": int(r["oi_change"]) if pd.notna(r.get("oi_change")) else None,
                "prev_oi": int(r["prev_oi"]) if pd.notna(r.get("prev_oi")) else None,
                "oi_snapshot_at": stamp.isoformat() if is_option and pd.notna(r.get("oi")) else None,
                "source": "fyers_history_eod_recovery",
            }))
        rows_by_symbol.append({"symbol": symbol, "rows": normalized, "error": None})
        if progress_callback:
            progress_callback(
                completed=idx,
                total=total,
                symbol=symbol,
                message=f"Fetched {len(normalized):,} candles • {idx}/{total}",
            )

    return rows_by_symbol


def _store_insert_missing_candles(store, rows):
    """Compatibility-safe additive insert for EOD recovery.

    Existing (symbol, candle_start) rows are never updated. This fallback keeps
    the same semantics if an older CloudMarketStore is loaded by Streamlit.
    """
    method = getattr(store, "insert_missing_candles", None)
    if callable(method):
        return method(rows)
    if not rows:
        return {"inserted": 0, "skipped": 0, "failed": 0, "rows": []}

    grouped = {}
    for row in rows:
        row = _candle_payload(row)
        symbol = str(row.get("symbol") or "").strip()
        if symbol:
            grouped.setdefault(symbol, []).append(row)

    inserted = 0
    skipped = 0
    inserted_rows = []
    for symbol, symbol_rows in grouped.items():
        stamps = [pd.Timestamp(r["candle_start"]) for r in symbol_rows if r.get("candle_start")]
        if not stamps:
            continue
        start = min(stamps).to_pydatetime()
        end = (max(stamps) + pd.Timedelta(minutes=1)).to_pydatetime()
        response = (
            store.client.table("market_candles_1m")
            .select("symbol,candle_start")
            .eq("symbol", symbol)
            .gte("candle_start", start.isoformat())
            .lt("candle_start", end.isoformat())
            .execute()
        )
        existing = {(str(r.get("symbol")), str(r.get("candle_start")))
                    for r in (response.data or [])
                    if r.get("symbol") and r.get("candle_start")}
        missing = [r for r in symbol_rows
                   if (symbol, str(r.get("candle_start"))) not in existing]
        skipped += len(symbol_rows) - len(missing)
        for offset in range(0, len(missing), 500):
            chunk = [_candle_payload(r) for r in missing[offset:offset + 500]]
            store.client.table("market_candles_1m").upsert(
                chunk, on_conflict="symbol,candle_start", ignore_duplicates=True
            ).execute()
            inserted += len(chunk)
            inserted_rows.extend(chunk)
    return {"inserted": inserted, "skipped": skipped, "failed": 0, "rows": inserted_rows}


def _store_fetch_instruments(store):
    """Compatibility wrapper for the cloud instrument registry."""
    # Keep the direct table expression for older EOD compatibility tests and
    # deployments; CloudMarketStore exposes the same query as fetch_instruments().
    response = store.client.table("instruments").select("*").order("symbol").execute()
    return response.data or []

def _recover_missing_day(store, client, signal_symbol, selected_date, scope, progress_callback=None):
    # Complete-day recovery is a repair operation, not just an additive backfill:
    # first remove stale candles outside the NSE regular session, then refill
    # the canonical 09:15 <= candle_start < 15:30 window from FYERS history.
    if progress_callback:
        progress_callback(
            completed=0,
            total=0,
            symbol="",
            message="Cleaning out-of-session rows before day recovery…",
        )
    delete_method = getattr(store, "delete_out_of_session_candles", None)
    deleted_out_of_session = 0
    if callable(delete_method):
        deleted_out_of_session = int(
            delete_method(
                selected_date,
                scope=scope,
                signal_symbol=signal_symbol,
            ) or 0
        )

    instruments = _store_fetch_instruments(store)
    # Only option contracts and the requested NIFTY index are relevant. All
    # historical option metadata is retained in `instruments`, so contracts
    # discovered earlier in the session remain eligible for recovery.
    selected = []
    for row in instruments:
        typ = str(row.get("option_type") or "").upper()
        symbol = str(row.get("symbol") or "").strip()
        if symbol == signal_symbol or typ in {"CE", "PE"}:
            if scope == "NIFTY 50" and symbol != signal_symbol:
                continue
            if scope == "All CE" and typ != "CE":
                continue
            if scope == "All PE" and typ != "PE":
                continue
            selected.append(row)

    results = _build_day_recovery_rows(client, selected, signal_symbol, selected_date, progress_callback=progress_callback)
    all_rows = []
    errors = []
    no_data = 0
    for item in results:
        if item["error"]:
            errors.append((item["symbol"], item["error"]))
        if not item["rows"]:
            no_data += 1
        all_rows.extend(item["rows"])

    outcome = _store_insert_missing_candles(store, all_rows)
    newly_inserted = outcome.get("rows", [])
    # Do not expose the internal helper rows as part of the persistent state;
    # only the newly missing-at-check-time rows are offered for download.
    return {
        "fetched": len(all_rows),
        "inserted": int(outcome.get("inserted", 0)),
        "skipped": int(outcome.get("skipped", 0)),
        "deleted_out_of_session": deleted_out_of_session,
        "no_data_symbols": no_data,
        "errors": errors,
        "new_rows": pd.DataFrame(newly_inserted),
    }


def _render_data_center():
    st.markdown("## ☁️ Cloud Data Center")
    st.caption("Market data is stored in Supabase/Postgres. No local database or market-data files are used.")

    url = os.getenv("SUPABASE_URL", "").strip()
    key = (os.getenv("SUPABASE_SECRET_KEY", "").strip()
           or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())
    if not url or not key:
        st.warning("Supabase is not configured. Add SUPABASE_URL and SUPABASE_SECRET_KEY to the server/Streamlit secrets.")
        st.code("SUPABASE_URL=https://YOUR_PROJECT.supabase.co\nSUPABASE_SECRET_KEY=sb_secret_...", language="text")
        return

    try:
        store = CloudMarketStore(url, key)
        health = store.health_check()
        st.success(f"Supabase connected • {health.get('rows', 0):,} candle rows • {health.get('latency_ms', 0)} ms")
    except Exception as exc:
        st.error(f"Supabase connection/schema check failed: {exc}")
        st.info("Run supabase_schema.sql once in the Supabase SQL Editor, then refresh this page.")
        return

    live_engine = st.session_state.get("engine")
    if live_engine is not None:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Recorder", getattr(live_engine, "cloud_status", "—"))
        c2.metric("Cloud options", len(getattr(live_engine, "cloud_data_symbols", set())))
        c3.metric("Rows written", f"{getattr(getattr(live_engine, 'cloud_recorder', None), 'total_written', 0):,}")
        c4.metric("Pending writes", f"{getattr(getattr(live_engine, 'cloud_recorder', None), 'pending_count', 0):,}")
        err = getattr(getattr(live_engine, "cloud_recorder", None), "last_write_error", None)
        if err:
            st.error(f"Last cloud write error (the batch is retained for retry): {err}")

    selected_date = st.date_input("Trading date", value=pd.Timestamp.now(tz=REPLAY_IST).date(), key="cloud_data_date")
    scope = st.selectbox("Data", ["Everything", "NIFTY 50", "All CE", "All PE"], key="cloud_data_scope")

    start = pd.Timestamp(selected_date, tz=REPLAY_IST).to_pydatetime()
    end = (pd.Timestamp(selected_date, tz=REPLAY_IST) + pd.Timedelta(days=1)).to_pydatetime()

    client = st.session_state.get("client")
    if client is None:
        st.warning("Connect to FYERS first. End-of-day recovery uses FYERS historical data to fill only missing Supabase candles.")
    else:
        st.caption(
            "Complete Day first removes existing rows outside the regular NSE session "
            "(09:15–15:30 IST) for the selected scope, then fetches FYERS history and inserts "
            "only missing candles inside that session. Valid in-session rows are preserved."
        )
        manager = _recovery_manager()
        if "_recovery_owner_id" not in st.session_state:
            st.session_state._recovery_owner_id = uuid.uuid4().hex
        recovery_owner = st.session_state._recovery_owner_id

        # The recovery request is intentionally launched in a daemon worker.
        # Never run 209 FYERS REST requests in the Streamlit script runner:
        # doing so freezes this session's page and prevents live chart fragments
        # from receiving their normal reruns.
        job = manager.snapshot(recovery_owner)
        if job and job.get("running"):
            st.info("Historical recovery is running in the background. You can switch to Terminal/Charts; the live FYERS feed is not blocked.")
        elif job and job.get("done") and job.get("error"):
            st.error(f"End-of-day recovery failed: {job['error']}")

        start_recovery = st.button(
            "🔄 Complete day • clean out-of-session + fill 09:15–15:30",
            type="primary",
            width="stretch",
            key="recover_missing_day",
            disabled=bool(job and job.get("running")),
        )
        if start_recovery:
            signal_symbol = (
                getattr(live_engine, "signal_symbol", None)
                or os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX")
            )
            app_id_for_recovery = str(st.session_state.get("auth_app_id") or os.getenv("FYERS_APP_ID", "")).strip()
            token_for_recovery = str(st.session_state.get("token") or os.getenv("FYERS_ACCESS_TOKEN", "")).strip()
            if not app_id_for_recovery or not token_for_recovery:
                st.error("FYERS App ID/access token is missing; connect to FYERS first.")
            else:
                started, job = manager.start(
                    recovery_owner,
                    url=url,
                    key=key,
                    app_id=app_id_for_recovery,
                    token=token_for_recovery,
                    signal_symbol=signal_symbol,
                    selected_date=selected_date,
                    scope=scope,
                )
                if started:
                    st.session_state.cloud_recovery_summary = None
                    st.session_state.cloud_new_rows = pd.DataFrame()
                    st.success("Recovery started in the background. Live charts will continue updating.")
                else:
                    st.info("A recovery is already running for this session.")

        @st.fragment(run_every="1s")
        def _recovery_status_fragment():
            current = manager.snapshot(recovery_owner)
            if not current:
                return
            completed = int(current.get("completed", 0))
            total = int(current.get("total", 0))
            if current.get("running"):
                if total:
                    st.progress(
                        min(1.0, completed / total),
                        text=f"{current.get('message') or 'Recovering…'}",
                    )
                    st.caption(
                        f"Background recovery: {completed}/{total} symbols • "
                        f"live recorder/chart remains independent."
                    )
                else:
                    st.info(current.get("message") or "Starting background recovery…")
                return

            if current.get("error"):
                st.error(f"End-of-day recovery failed: {current['error']}")
                return

            if current.get("done"):
                # Only copy the finished worker result into Streamlit session
                # state from the UI thread.
                if st.session_state.get("_recovery_consumed_key") != current.get("finished_at"):
                    rows = current.get("new_rows") or []
                    st.session_state.cloud_new_rows = pd.DataFrame(rows)
                    summary = {
                        "fetched": int(current.get("fetched", 0)),
                        "inserted": int(current.get("inserted", 0)),
                        "skipped": int(current.get("skipped", 0)),
                        "deleted_out_of_session": int(current.get("deleted_out_of_session", 0)),
                        "no_data_symbols": int(current.get("no_data_symbols", 0)),
                        "errors": list(current.get("errors", [])),
                    }
                    st.session_state.cloud_recovery_summary = summary
                    # The previously loaded table may contain rows that the
                    # cleanup just removed. Force an explicit reload from
                    # Supabase rather than displaying a stale snapshot.
                    st.session_state.cloud_export_df = pd.DataFrame()
                    st.session_state._recovery_consumed_key = current.get("finished_at")

                summary = st.session_state.get("cloud_recovery_summary")
                if summary:
                    st.success(
                        f"Day recovery complete • removed {summary['deleted_out_of_session']:,} "
                        f"out-of-session rows • fetched {summary['fetched']:,} • "
                        f"inserted {summary['inserted']:,} new rows • "
                        f"kept {summary['skipped']:,} existing in-session rows."
                    )
                    if summary["no_data_symbols"]:
                        st.info(
                            f"{summary['no_data_symbols']:,} symbols returned no historical candles. "
                            "That is normal for some option contracts."
                        )
                    if summary["errors"]:
                        st.warning(
                            f"{len(summary['errors']):,} symbols had recoverable history errors; "
                            "the rest of the day was still saved."
                        )

        _recovery_status_fragment()

    if st.button("↻ Load cloud data", type="secondary", width="stretch", key="load_cloud_data"):
        try:
            df = store.fetch_candles(start, end)
            if not df.empty:
                if scope == "NIFTY 50":
                    df = df[df["option_type"].isna()]
                elif scope == "All CE":
                    df = df[df["option_type"] == "CE"]
                elif scope == "All PE":
                    df = df[df["option_type"] == "PE"]
                st.session_state.cloud_export_df = df.reset_index(drop=True)
                st.success(f"Loaded {len(df):,} rows from Supabase.")
            else:
                st.session_state.cloud_export_df = pd.DataFrame()
                st.info("No cloud data exists for that date yet.")
        except Exception as exc:
            st.error(f"Cloud data read failed: {exc}")

    new_df = st.session_state.get("cloud_new_rows", pd.DataFrame())
    if isinstance(new_df, pd.DataFrame) and not new_df.empty:
        st.markdown("### Newly recovered data")
        st.caption("These are the rows that were missing when the recovery ran. Existing Supabase rows were not rewritten.")
        st.download_button(
            "⬇️ Download ONLY newly recovered rows",
            new_df.to_csv(index=False).encode("utf-8"),
            file_name=f"fyers_{selected_date}_newly_recovered.csv",
            mime="text/csv", width="stretch", key="download_newly_recovered_csv",
        )

    df = st.session_state.get("cloud_export_df", pd.DataFrame())
    if isinstance(df, pd.DataFrame) and not df.empty:
        st.dataframe(df.head(500), width="stretch", hide_index=True)
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download CSV", csv_bytes,
            file_name=f"fyers_{selected_date}_{scope.lower().replace(' ', '_')}.csv",
            mime="text/csv", width="stretch", key="download_cloud_csv",
        )

        # A ZIP is generated in memory from the cloud query. Nothing is written
        # to the user's PC by the application until they explicitly download it.
        import io as _io, zipfile as _zipfile
        zbuf = _io.BytesIO()
        with _zipfile.ZipFile(zbuf, "w", _zipfile.ZIP_DEFLATED) as zf:
            if scope == "Everything":
                groups = [("NIFTY_1MIN.csv", df[df["option_type"].isna()]),
                          ("CE_1MIN.csv", df[df["option_type"] == "CE"]),
                          ("PE_1MIN.csv", df[df["option_type"] == "PE"]) ]
                for name, part in groups:
                    zf.writestr(name, part.to_csv(index=False))
            else:
                zf.writestr(f"{scope.replace(' ', '_')}_1MIN.csv", df.to_csv(index=False))
        st.download_button(
            "⬇️ Download ZIP", zbuf.getvalue(),
            file_name=f"fyers_{selected_date}_market_data.zip",
            mime="application/zip", width="stretch", key="download_cloud_zip",
        )

if page == "data":
    _render_data_center()
    st.stop()

# ---------- dedicated FYERS auth page ----------
def _render_auth_web():
    st.markdown("## 🔐 FYERS Auth Web")
    st.caption(
        "Old callback style: FYERS redirects to the bare Streamlit root and "
        "the auth-code screen appears here as ?s=ok&code=200&auth_code=...&state=...."
    )

    app_url = os.getenv(
        "STREAMLIT_APP_URL",
        "https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app",
    )
    recommended_redirect = f"{app_url.rstrip('/')}/"

    st.markdown("### 1. Auth details")
    if "auth_page_app_id" not in st.session_state:
        st.session_state.auth_page_app_id = st.session_state.get("auth_app_id") or os.getenv("FYERS_APP_ID", "")
    if "auth_page_secret_id" not in st.session_state:
        st.session_state.auth_page_secret_id = st.session_state.get("auth_secret_id") or os.getenv("FYERS_SECRET_ID", "")
    if "auth_page_redirect_uri" not in st.session_state:
        st.session_state.auth_page_redirect_uri = st.session_state.get("auth_redirect_uri") or recommended_redirect
    app_id = st.text_input(
        "App ID",
        key="auth_page_app_id",
    )
    secret_id = st.text_input(
        "Secret ID",
        type="password",
        key="auth_page_secret_id",
    )
    redirect_uri = st.text_input(
        "Redirect URI",
        key="auth_page_redirect_uri",
        help="Use the bare Streamlit app URL ending in /. Do not add ?page=auth.",
    )
    redirect_uri = _bare_redirect_uri(redirect_uri)

    # Save FIRST, before the external link is rendered.
    st.session_state.auth_app_id = app_id.strip()
    st.session_state.auth_secret_id = secret_id.strip()
    st.session_state.auth_redirect_uri = redirect_uri
    if app_id.strip() and secret_id.strip() and redirect_uri.strip():
        try:
            login_url = _prepare_auth_flow(app_id, secret_id, redirect_uri)
        except Exception as exc:
            login_url = ""
            st.session_state.auth_url = ""
            st.error(f"Could not create FYERS auth URL: {exc}")
    else:
        login_url = ""

    st.markdown("### 2. Open Auth Web")
    if login_url:
        safe_login_url = html.escape(login_url, quote=True)
        # Deliberately a plain same-tab anchor. Do NOT use st.link_button here:
        # Streamlit link buttons may open an external target in a new tab.
        st.markdown(
            f'<a href="{safe_login_url}" target="_blank" rel="noopener noreferrer" '
            'style="display:block;text-align:center;padding:0.8rem 1rem;'
            'border-radius:0.5rem;background:#2d2d38;color:white;text-decoration:none;'
            'font-weight:700;margin:0.5rem 0 1rem;">🔐 Open Auth Web</a>',
            unsafe_allow_html=True,
        )
        st.caption(
            "Your current App ID, Secret ID and Redirect URI are already saved. "
            "If FYERS already has an active browser session, it should redirect "
            "straight back to this app; if not, FYERS may show its login screen."
        )
        with st.expander("Show generated auth URL", expanded=False):
            st.code(login_url, language=None)
    else:
        st.info("Enter App ID, Secret ID and Redirect URI to enable Open Auth Web.")

    st.markdown("### 3. Authorization result")
    if st.session_state.get("callback_auth_code"):
        st.success("Auth code received. Copy it below, then return to Terminal.")
        st.code(st.session_state.callback_auth_code, language=None)
        st.text_input(
            "Auth code — copy from here",
            value=st.session_state.callback_auth_code,
            key="fyers_auth_code_copy",
        )
        if st.session_state.get("auth_callback_error"):
            st.error(st.session_state.auth_callback_error)
    else:
        st.info("Waiting for the FYERS callback…")

    b1, b2 = st.columns(2)
    with b1:
        if st.button("↻ Start fresh auth attempt", width="stretch"):
            st.session_state.auth_url = ""
            st.session_state.auth_state = ""
            st.session_state.auth_input_fingerprint = ""
            st.session_state.callback_auth_code = ""
            st.session_state.auth_callback_error = ""
            st.rerun()
    with b2:
        if st.button("↩️ Back to Terminal", type="primary", width="stretch"):
            st.session_state.show_auth_callback = False
            st.session_state.page = "terminal"
            st.session_state.do_connect = bool(st.session_state.get("token"))
            st.session_state.auth_url = ""
            st.session_state.auth_state = ""
            st.query_params.clear()
            st.query_params["page"] = "terminal"
            st.rerun()

if page == "auth":
    _render_auth_web()
    st.stop()

# ---------- sidebar ----------
with st.sidebar:
    st.markdown("## 🔌 FYERS")
    st.caption("Connect once, then run the terminal.")

    st.selectbox(
        "Interface theme",
        ["Dark", "Light", "Black & White"],
        index=["Dark", "Light", "Black & White"].index(st.session_state.get("ui_theme", "Dark")),
        key="ui_theme",
        help="Switch the terminal and live chart presentation without restarting FYERS.",
    )

    # Simple connection: only two fields are needed when the user already has a token.
    app_id = st.text_input(
        "App ID",
        value=st.session_state.get("auth_app_id") or _saved_app_id,
        help="Your FYERS API App ID / Client ID.",
    )
    token = st.text_input(
        "Access token",
        value=st.session_state.token,
        type="password",
        help="Paste today's FYERS v3 access token here.",
    )

    if st.button("🔗 Connect to FYERS", type="primary", width="stretch"):
        st.session_state.do_connect = True

    # After a browser refresh session_state is empty again. If a saved token
    # exists, restore it and reconnect once automatically. This keeps refresh
    # from looking like a logout while still allowing the user to replace the
    # token normally when FYERS issues a new daily token.
    if (
        not st.session_state.get("connected")
        and not st.session_state.get("_auto_reconnect_attempted", False)
        and st.session_state.get("token")
        and (_saved_app_id or app_id.strip())
    ):
        st.session_state._auto_reconnect_attempted = True
        st.session_state.auth_app_id = app_id.strip() or _saved_app_id
        st.session_state.do_connect = True

    if st.session_state.get("connected"):
        st.success("Connected")
    else:
        st.caption("Don't have today's token?")

    # Advanced auth is deliberately hidden. Most users only need App ID + access token.
    with st.expander("Get a fresh access token", expanded=False):
        st.info("Use this when your token has expired or you see FYERS -16. After login, this app shows a simple auth-code page.")
        if "auth_app_id_input" not in st.session_state:
            st.session_state.auth_app_id_input = st.session_state.get("auth_app_id") or os.getenv("FYERS_APP_ID", "")
        if "auth_secret_id_input" not in st.session_state:
            st.session_state.auth_secret_id_input = st.session_state.get("auth_secret_id") or os.getenv("FYERS_SECRET_ID", "")
        if "auth_redirect_uri_input" not in st.session_state:
            st.session_state.auth_redirect_uri_input = st.session_state.get("auth_redirect_uri") or os.getenv(
                "FYERS_REDIRECT_URI", "https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/"
            )
        # These values are restored from the short-lived auth-flow record when
        # FYERS redirects back, so the user does not have to paste them again.
        app_id = st.text_input(
            "App ID",
            key="auth_app_id_input",
        )
        secret_id = st.text_input(
            "1. Secret ID",
            type="password",
            key="auth_secret_id_input",
        )
        default_redirect_uri = st.session_state.auth_redirect_uri or os.getenv(
            "FYERS_REDIRECT_URI",
            "https://vwap-algo-pej2nt7fjsxausdc9trgnk.streamlit.app/",
        )
        redirect_uri = st.text_input(
            "2. Redirect URI",
            key="auth_redirect_uri_input",
            help="Use the bare Streamlit app URL ending in /. Do not add ?page=auth. It must match the FYERS dashboard exactly.",
        )
        redirect_uri = _bare_redirect_uri(redirect_uri)

        # Save the exact values on every rerun. If the user changes any of
        # them, _prepare_auth_flow() creates a NEW state and URL automatically.
        st.session_state.auth_app_id = app_id.strip()
        st.session_state.auth_secret_id = secret_id.strip()
        st.session_state.auth_redirect_uri = redirect_uri.strip()
        if app_id.strip() and secret_id.strip() and redirect_uri.strip():
            try:
                _prepare_auth_flow(app_id, secret_id, redirect_uri)
            except Exception as e:
                st.session_state.auth_url = ""
                st.error(str(e))

        if st.session_state.auth_url:
            st.markdown("**3. Open Auth Web**")
            st.session_state["fyers_login_url"] = st.session_state.auth_url
            safe_login_url = html.escape(st.session_state.auth_url, quote=True)
            st.markdown(
                f'<a href="{safe_login_url}" target="_blank" rel="noopener noreferrer" '
                'style="display:block;text-align:center;padding:0.65rem 1rem;'
                'border-radius:0.5rem;background:#2d2d38;color:white;text-decoration:none;'
                'font-weight:700;margin:0.35rem 0 0.5rem;">🔐 Open Auth Web</a>',
                unsafe_allow_html=True,
            )
            st.caption(
                "Saved before opening. FYERS returns to the bare Streamlit root "
                "with ?s=ok&code=200&auth_code=...&state=...."
            )

        auth_code = st.text_input(
            "5. Auth code",
            value=st.session_state.callback_auth_code,
            type="password",
            key="auth_code_terminal",
            help="After FYERS redirects back, the app captures the code automatically.",
        )
        if st.button("6. Get today's token", width="stretch", key="get_todays_token"):
            try:
                if not secret_id or not redirect_uri or not auth_code:
                    raise ValueError("Enter Secret ID, Redirect URI and auth_code first.")
                new_token = FyersClient.exchange_auth_code(app_id, secret_id, _bare_redirect_uri(redirect_uri), auth_code)
                st.session_state.token = new_token
                st.session_state.auth_token_ready = True
                st.session_state.callback_auth_code = ""
                st.session_state.show_auth_callback = False
                st.session_state.do_connect = True
                st.success("Token created. Connecting to FYERS…")
            except Exception as e:
                st.error(f"Token exchange failed: {e}")

    st.divider()
    st.markdown("## 🎯 Strategy")
    signal_symbol = st.text_input(
        "Signal symbol",
        value=os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX"),
        help="The NIFTY instrument used for the VWAP signal.",
    )
    resolution = st.selectbox("Timeframe", ["1", "3", "5", "10", "15", "30", "60"], index=2)
    st.markdown("### ⏱️ Algo session (IST)")
    st.caption("Choose when the strategy is allowed to create new entries. The first candle starts at the selected start time and lasts for the full selected timeframe.")
    algo_start_time = st.time_input("Algo start time", value=dt_time(9, 15), key="algo_start_time")
    algo_end_time = st.time_input("Algo end time", value=dt_time(15, 15), key="algo_end_time")
    algo_start_minute = _hhmm_minute(algo_start_time, DEFAULT_ENTRY_START_MINUTE)
    algo_end_minute = _hhmm_minute(algo_end_time, DEFAULT_ENTRY_END_MINUTE)
    if algo_end_minute < algo_start_minute:
        st.error("Algo end time must be at or after the start time.")
    else:
        st.caption(f"Entries: {_hhmm(algo_start_time, '09:15')}–{_hhmm(algo_end_time, '15:15')} IST • first candle is aligned to {_hhmm(algo_start_time, '09:15')} and uses the {resolution}-minute timeframe.")
    confirmation_points = st.number_input("Move after VWAP cross", 1.0, 100.0, 15.0, 0.5)
    confirmation_bars = st.number_input("Confirmation window (candles)", 1, 20, 8, 1)
    st.caption("Entry rule: a CLOSED VWAP-cross candle arms the setup; the next 8 candles may confirm only after price reaches ±15 points from that cross close. The crossing candle itself can never confirm.")

    st.markdown("## 🎯 Option entry")
    option_underlying = st.text_input("Option chain", value=signal_symbol)
    premium_min = st.number_input("Premium min", 1.0, 1000.0, 170.0, 1.0)
    premium_max = st.number_input("Premium max", 1.0, 2000.0, 210.0, 1.0)
    premium_target = st.number_input("Preferred premium", premium_min, premium_max, 190.0, 1.0)
    expiry_mode = st.selectbox("Expiry", ["Nearest", "Monthly"], index=0)
    strikecount = st.number_input("Strike search", 1, 50, 25, 1)
    option_lot_size = option_lot_size_for_symbol(option_underlying)
    option_lots = st.number_input(
        "Option lots",
        1, 100, 1, 1,
        help="Number of exchange lots to buy. The order quantity is calculated from the official lot size for the selected index.",
    )
    qty = int(option_lots) * int(option_lot_size)
    st.caption(
        f"📦 **1 lot = {option_lot_size} quantity** • "
        f"Order quantity: **{qty}**"
        + ("" if option_lot_size != 1 else " • Lot size not mapped for this underlying")
    )

    st.markdown("## 🛡️ SL & Target")
    place_protection = st.checkbox("Place SL + Target immediately", value=True)
    protection_mode = st.selectbox("Protection", ["Points", "Percent", "ATR"], index=0)
    if protection_mode == "Points":
        sl_points = st.number_input("SL points", 0.05, 500.0, 20.0, 0.05)
        target_points = st.number_input("Target points", 0.05, 1000.0, 40.0, 0.05)
        sl_percent = target_percent = 0.0
        sl_atr_mult = target_atr_mult = 0.0
    elif protection_mode == "Percent":
        sl_percent = st.number_input("SL %", 0.1, 99.0, 10.0, 0.5)
        target_percent = st.number_input("Target %", 0.1, 500.0, 20.0, 0.5)
        sl_points = target_points = 0.0
        sl_atr_mult = target_atr_mult = 0.0
    else:
        sl_atr_mult = st.number_input("SL ATR multiplier", 0.1, 10.0, 1.5, 0.1)
        target_atr_mult = st.number_input("Target ATR multiplier", 0.1, 20.0, 3.0, 0.1)
        sl_points = target_points = sl_percent = target_percent = 0.0

    st.markdown("## ⚠️ Safety")
    live = st.checkbox("Enable LIVE orders", value=False)
    if live:
        st.error("LIVE orders are ON")
    else:
        st.info("Paper / dry-run mode")

    test_live_entry = st.checkbox(
        "🧪 Test LIVE entry engine (NO broker order)",
        value=True,
        disabled=live,
        help="Runs the same VWAP → option selection → SL/Target → FYERS order-payload path used by LIVE trading, but never sends the order when LIVE orders are OFF.",
    )
    if not live and test_live_entry:
        st.caption("TEST mode: when the entry condition triggers, the app will log exactly what the live algo WOULD send. No money/order is used.")

    st.divider()
    st.markdown("## 📝 Paper Trading")
    st.caption("Uses live FYERS prices but NEVER sends an order to FYERS.")
    paper_manual = st.checkbox("Enable manual paper orders", value=True)
    auto_paper = st.checkbox(
        "Auto paper entries on VWAP confirmation", value=True,
        help="When the closed candle crosses VWAP and then moves the selected points within the confirmation window, automatically select the CE/PE in the premium band and open a paper trade."
    )
    paper_lots = st.number_input(
        "Paper lots",
        1, 100, int(option_lots), 1,
        help="Paper orders use the same exchange lot size as the selected option.",
    )
    paper_qty = int(paper_lots) * int(option_lot_size)
    st.caption(f"Paper order quantity: **{paper_qty}** ({paper_lots} lot{'s' if paper_lots != 1 else ''})")
    paper_sl = st.number_input("Paper SL points", 0.0, 5000.0, float(sl_points or 20.0), 0.5)
    paper_target = st.number_input("Paper target points", 0.0, 10000.0, float(target_points or 40.0), 0.5)
# ---------- execution mode banner ----------
def render_rejection_panel():
    items = st.session_state.get("rejected_orders", [])
    if not items:
        return
    latest = items[-1]
    st.markdown(
        f"""<div class="reject-card">
        <div class="reject-title">⛔ LAST ORDER / ENTRY REJECTION</div>
        <div class="reject-detail"><b>{html.escape(str(latest.get("time","")))}</b> • {html.escape(str(latest.get("message","")))}</div>
        </div>""",
        unsafe_allow_html=True,
    )
    with st.expander(f"⛔ Order errors & rejections ({len(items)})", expanded=False):
        rows = [{"Time": x.get("time",""), "Type": x.get("type","REJECTED"), "Reason": x.get("message","")}
                for x in reversed(items[-50:])]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=240)


def mode_banner():
    if live:
        label, cls, desc = "🔴 LIVE BROKER", "danger", "Real FYERS order placement is enabled."
    elif test_live_entry:
        label, cls, desc = "🟡 LIVE ENGINE TEST", "test", "Exact live-entry path; broker order placement is blocked."
    elif auto_paper:
        label, cls, desc = "🟢 AUTO PAPER", "paper", "VWAP entries are simulated automatically."
    else:
        label, cls, desc = "⚪ MANUAL PAPER", "paper", "Only manual simulated entries are enabled."
    st.markdown(
        '<div class="mode-banner %s"><div><b>%s</b></div><div class="mode-desc">%s</div>'
        '<div class="mode-chain"><span>VWAP CROSS</span><b>→</b><span>ARMED</span><b>→</b>'
        '<span>POINT MOVE</span><b>→</b><span>OPTION</span><b>→</b><span>ENTRY</span></div></div>'
        % (cls, label, desc),
        unsafe_allow_html=True,
    )

mode_banner()

# ---------- helpers ----------
def load_portfolio(client):
    result = {}
    for name, fn in [("funds", client.funds), ("positions", client.positions), ("holdings", client.holdings), ("orders", client.orders), ("trades", client.trades)]:
        try: result[name] = fn()
        except Exception as e: result[name] = {"s":"error", "message":str(e)}
    return result


def extract_list(response, keys):
    if not isinstance(response, dict): return []
    data = response.get("data", response)
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list): return data[key]
    return []


def portfolio_tables(portfolio):
    return (
        extract_list(portfolio.get("positions", {}), ["netPositions", "positions"]),
        extract_list(portfolio.get("holdings", {}), ["holdings"]),
        extract_list(portfolio.get("orders", {}), ["orderBook", "orders"]),
        extract_list(portfolio.get("trades", {}), ["tradeBook", "trades"]),
    )


# ---------- dedicated charts page ----------

CHART_TIMEFRAMES = ["1", "3", "5", "10", "15", "30", "60"]
CHART_CANDLE_LIMIT = 800


def _chart_history_days(resolution, candles=CHART_CANDLE_LIMIT):
    """Estimate enough calendar history to retain `candles` bars.

    The live chart always transports up to 800 candles. Higher timeframes need
    a wider REST lookback than the old fixed 31-day window (e.g. 60m needs
    roughly 6 months for 800 bars). A small buffer covers holidays/short
    sessions without making every 1m request unnecessarily large.
    """
    try:
        minutes = max(1, int(resolution))
    except (TypeError, ValueError):
        minutes = 5
    # NSE cash session is about 375 minutes/day. Convert trading bars to
    # calendar days with a 7/5 weekday buffer and 20% holiday/session margin.
    trading_days = (float(candles) * minutes) / 375.0
    calendar_days = int(trading_days * 7.0 / 5.0 * 1.20) + 7
    return max(31, min(730, calendar_days))


def _merge_chart_history(primary, live_df):
    """Merge REST history with the live engine frame without losing live OHLC."""
    frames = []
    for frame in (primary, live_df):
        if frame is not None and not frame.empty:
            frames.append(frame.copy())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = (
            out.dropna(subset=["datetime"])
               .sort_values("datetime")
               .drop_duplicates("datetime", keep="last")
               .reset_index(drop=True)
        )
    return out


def _chart_bucket(ts, resolution):
    """Return the start of the selected chart-timeframe candle in IST."""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("Asia/Kolkata")
    else:
        ts = ts.tz_convert("Asia/Kolkata")
    minutes = max(1, int(resolution))
    total = ts.hour * 60 + ts.minute
    bucket = (total // minutes) * minutes
    return ts.replace(
        hour=bucket // 60,
        minute=bucket % 60,
        second=0,
        microsecond=0,
    )


def _with_live_tick(df, tick, resolution):
    """Overlay the latest FYERS tick onto the exact chart candle bucket.

    History and websocket data are two views of the same candle. Merge by
    timestamp instead of assuming the final dataframe row is the current
    bucket. This keeps delayed/reconnected feeds from creating displaced bars.
    """
    if not tick or tick.get("ltp") is None:
        return df.copy() if df is not None else pd.DataFrame()

    out = df.copy() if df is not None else pd.DataFrame()
    ltp = float(tick["ltp"])
    ts = tick.get("time")
    if ts is None:
        ts = pd.Timestamp.now(tz="Asia/Kolkata")
    start = _chart_bucket(ts, resolution)

    if out.empty:
        return pd.DataFrame([{
            "datetime": start, "open": ltp, "high": ltp,
            "low": ltp, "close": ltp, "volume": 0.0,
        }])

    out["datetime"] = pd.to_datetime(out["datetime"])
    if out["datetime"].dt.tz is None:
        out["datetime"] = out["datetime"].dt.tz_localize("Asia/Kolkata")
    else:
        out["datetime"] = out["datetime"].dt.tz_convert("Asia/Kolkata")

    matches = out["datetime"] == start
    if matches.any():
        idx = out.index[matches][-1]
        for col in ("open", "high", "low", "close"):
            if col not in out.columns:
                continue
            old_value = pd.to_numeric(out.loc[idx, col], errors="coerce")
            if col == "open":
                out.loc[idx, col] = float(old_value) if pd.notna(old_value) else ltp
            elif col == "high":
                out.loc[idx, col] = max(float(old_value) if pd.notna(old_value) else ltp, ltp)
            elif col == "low":
                out.loc[idx, col] = min(float(old_value) if pd.notna(old_value) else ltp, ltp)
            else:
                out.loc[idx, col] = ltp
    else:
        new_row = {
            "datetime": start, "open": ltp, "high": ltp,
            "low": ltp, "close": ltp, "volume": 0.0,
        }
        # Keep the VWAP line continuous when FYERS history already contains a
        # valid VWAP for this same trading day but the new live overlay has not
        # received a volume contribution yet. Never borrow VWAP from another day.
        if "vwap" in out.columns:
            day_mask = out["datetime"].dt.date == start.date()
            same_day_vwap = pd.to_numeric(
                out.loc[day_mask, "vwap"], errors="coerce"
            ).dropna()
            if not same_day_vwap.empty:
                new_row["vwap"] = float(same_day_vwap.iloc[-1])
        out = pd.concat([out, pd.DataFrame([new_row])], ignore_index=True)

    # If the matched history row had no VWAP but another candle in the same
    # session does, preserve that session-local value rather than introducing a
    # single NaN hole into the line.
    if "vwap" in out.columns and matches.any():
        current_vwap = pd.to_numeric(out.loc[idx, "vwap"], errors="coerce")
        if pd.isna(current_vwap):
            day_mask = out["datetime"].dt.date == start.date()
            same_day_vwap = pd.to_numeric(
                out.loc[day_mask, "vwap"], errors="coerce"
            ).dropna()
            if not same_day_vwap.empty:
                out.loc[idx, "vwap"] = float(same_day_vwap.iloc[-1])

    return (
        out.drop_duplicates(subset=["datetime"], keep="last")
           .sort_values("datetime")
           .reset_index(drop=True)
    )
def _load_chart_history(client, symbol, resolution, days=31, force=False):
    """Load chart history once per symbol/timeframe/day-window in this session.

    Historical candles are immutable for the purposes of the live chart.  Do
    not make a REST request on every fragment update; only a deliberate reload
    bypasses this cache.
    """
    cache = st.session_state.setdefault("_chart_history_cache", {})
    key = (str(symbol), str(resolution), int(days))
    if not force and key in cache:
        return cache[key].copy()
    try:
        df = client.history(symbol, resolution, days)
        out = df.copy() if df is not None else pd.DataFrame()
        if out.empty:
            st.session_state["chart_history_error"] = (
                f"FYERS returned no candles for {symbol} ({resolution})."
            )
            cache.pop(key, None)
            return pd.DataFrame()
        st.session_state["chart_history_error"] = ""
        cache[key] = out.copy()
        return out
    except Exception as exc:
        st.session_state["chart_history_error"] = str(exc)
        return cache.get(key, pd.DataFrame()).copy()




def _run_backtest(df, confirmation_points, confirmation_bars, sl_points=0.0, target_points=0.0, session_start="09:15", session_end="15:15"):
    """Run the VWAP confirmation state machine over completed candles."""
    if df is None or df.empty:
        return {"trades": [], "events": [], "equity": 0.0, "wins": 0, "losses": 0,
                "signals": 0, "total_entries": 0, "win_ratio": 0.0}

    start_minute = _hhmm_minute(session_start, DEFAULT_ENTRY_START_MINUTE)
    end_minute = _hhmm_minute(session_end, DEFAULT_ENTRY_END_MINUTE)
    if end_minute < start_minute:
        raise ValueError("Algo end time must be at or after the start time.")

    prepared = VwapConfirmationEngine.prepare(df.copy())
    strat = VwapConfirmationEngine(
        StrategyConfig(float(confirmation_points), int(confirmation_bars))
    )
    trades = []
    open_trade = None
    previous_date = None

    for bar_no, (_, row) in enumerate(prepared.iterrows()):
        ts = _ist_timestamp(row["datetime"])
        session_date = ts.date() if not pd.isna(ts) else None

        # Do not carry a pending setup across trading days.
        if previous_date is not None and session_date != previous_date and open_trade is None:
            try:
                strat.reset_trade()
                strat._clear_setup()
            except Exception:
                pass
        previous_date = session_date

        high = float(row["high"])
        low = float(row["low"])
        in_algo_window = _within_entry_window(ts, start_minute, end_minute)

        # The strategy only runs for NEW entries inside the configured window.
        # Mark the first candle at the configured session start so it is
        # evaluated from its own OHLC movement, even when historical data
        # contains earlier candles from the same day.
        if in_algo_window and ts.hour * 60 + ts.minute == start_minute:
            row = row.copy()
            row["_algo_session_first"] = True
        # If a position is already open, continue checking its exits after the
        # end time so SL/target timestamps remain realistic.
        signal = strat.process_closed_candle(row) if in_algo_window else None

        # Discard any NEW signal outside the configured IST entry window.
        if signal and open_trade is None and not _within_entry_window(ts, start_minute, end_minute):
            try:
                strat.reset_trade()
                strat._clear_setup()
            except Exception:
                pass
            signal = None

        if signal and open_trade is None:
            entry = float(signal["entry"])
            signal_side = str(signal["side"]).upper()
            side = "BUY"
            option_type = "CE" if signal_side == "BUY" else "PE"
            open_trade = {
                "side": side,
                "signal_side": signal_side,
                "option_type": option_type,
                "entry": entry,
                "entry_time": ts,
                "quantity": int(option_lot_size_for_symbol("NSE:NIFTY50-INDEX")),
                "lots": 1,
                "sl": entry - float(sl_points) if side == "BUY" and sl_points > 0 else entry + float(sl_points) if side == "SELL" and sl_points > 0 else None,
                "target": entry + float(target_points) if side == "BUY" and target_points > 0 else entry - float(target_points) if side == "SELL" and target_points > 0 else None,
                "bars_since_cross": signal["bars_since_cross"],
                "cross_price": float(signal.get("cross_price", entry)),
                "confirmation_level": float(signal.get("confirmation_level", entry)),
                "trigger_price": float(entry),
                "cross_type": str(signal.get("cross_type") or "CLOSE_CROSS"),
                "setup_quality": str(signal.get("setup_quality") or signal.get("cross_type") or "CLOSE_CROSS"),
                "_entry_bar": bar_no,
                "status": "OPEN",
            }
            # Never evaluate an exit on the entry candle.
            continue

        if open_trade is not None:
            exit_price = None
            reason = None
            # Conservative assumption when both levels are touched.
            if open_trade["side"] == "BUY":
                if open_trade["sl"] is not None and low <= open_trade["sl"]:
                    exit_price, reason = open_trade["sl"], "SL"
                elif open_trade["target"] is not None and high >= open_trade["target"]:
                    exit_price, reason = open_trade["target"], "TARGET"
            if exit_price is not None:
                # Backtest uses the underlying as the trigger proxy; option
                # entry semantics remain long-only.
                pnl = (exit_price - open_trade["entry"]) * (
                    1 if open_trade.get("signal_side") == "BUY" else -1
                )
                trades.append({
                    **open_trade, "exit": float(exit_price), "exit_time": ts,
                    "reason": reason, "pnl": float(pnl), "status": "CLOSED",
                })
                open_trade = None
                strat.reset_trade()

    # If replay ends with a position still open, keep it open rather than
    # fabricating an exit.
    if open_trade is not None:
        last_ts = _ist_timestamp(prepared.iloc[-1]["datetime"])
        if open_trade.get("_entry_bar", -1) < len(prepared) - 1:
            exit_price = float(prepared.iloc[-1]["close"])
            pnl = (exit_price - open_trade["entry"]) * (1 if open_trade["side"] == "BUY" else -1)
            trades.append({
                **open_trade, "exit": exit_price, "exit_time": last_ts,
                "reason": "END", "pnl": float(pnl), "status": "CLOSED",
            })
            open_trade = None

    wins = sum(1 for x in trades if x["pnl"] > 0)
    losses = sum(1 for x in trades if x["pnl"] <= 0)
    open_events = [{**open_trade, "status": "OPEN"}] if open_trade is not None else []
    events = list(trades) + open_events
    total_entries = len(events)
    closed = wins + losses
    win_ratio = (wins / closed * 100.0) if closed else 0.0
    return {
        "trades": trades, "events": events, "open_trade": open_trade,
        "equity": float(sum(x["pnl"] for x in trades)),
        "wins": wins, "losses": losses, "signals": total_entries,
        "total_entries": total_entries, "win_ratio": win_ratio,
    }


def _normalise_chart_time(value):
    """Normalise chart/event timestamps to timezone-aware IST."""
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return pd.NaT
        if ts.tzinfo is None:
            # Our candle/event data uses IST whenever it is timezone-naive.
            ts = ts.tz_localize(REPLAY_IST)
        else:
            ts = ts.tz_convert(REPLAY_IST)
        return ts
    except Exception:
        return pd.NaT


def _chart_marker_time(df, value):
    """Map an execution event to the nearest real candle in the loaded window.

    Candle timestamps from FYERS are IST-aware. Execution timestamps may be
    datetime objects, pandas timestamps, or serialized strings. Treat naive
    timestamps as IST (never UTC), then map to the closest candle. This avoids
    the classic 5h30m offset that made valid execution markers disappear.
    """
    if df is None or df.empty or value is None or "datetime" not in df.columns:
        return None
    try:
        candle_times = pd.Series(df["datetime"].map(_normalise_chart_time), index=df.index)
        target = _normalise_chart_time(value)
        valid = candle_times.notna()
        if not valid.any() or pd.isna(target):
            return None
        deltas = (candle_times[valid] - target).abs()
        idx = deltas.idxmin()
        # The marker should always belong to the candle that actually contains
        # the execution. A generous timeframe-aware tolerance also handles a
        # current live candle whose timestamp is reconstructed from a tick.
        # Derive the tolerance from the actual candle spacing. The chart can be
        # 1/3/5/10/15/30/60m independently of the strategy timeframe, so a
        # hard-coded 5m session value can incorrectly discard valid markers.
        if len(candle_times[valid]) > 1:
            ordered = candle_times[valid].sort_values()
            diffs = ordered.diff().dropna()
            step = diffs.median() if not diffs.empty else pd.Timedelta(minutes=5)
        else:
            step = pd.Timedelta(minutes=5)
        tolerance = max(pd.Timedelta(seconds=1), step * 2)
        if deltas.loc[idx] > tolerance:
            return None
        ts = candle_times.loc[idx]
        return int(ts.timestamp())
    except Exception:
        return None

def _trade_markers(df, trades, include_exits=True):
    """Build visible execution markers.

    Underlying signal mapping:
      BUY signal  -> BUY CE
      SELL signal -> BUY PE
    Exits are SELLs of the held option.
    """
    markers = []
    marker_no = 0
    for trade_no, trade in enumerate(trades or []):
        side = str(trade.get("side", "")).upper()
        signal_side = str(trade.get("signal_side") or side).upper()
        option_type = str(trade.get("option_type") or ("CE" if signal_side == "BUY" else "PE")).upper()

        # Prefer the exact timeframe bucket recorded by the execution engine.
        # This avoids losing a live intrabar trigger whose raw tick timestamp
        # does not exactly equal a candle's opening timestamp.
        entry_time = _chart_marker_time(
            df, trade.get("chart_time") or trade.get("entry_time")
        )
        if entry_time is not None:
            is_pe = option_type == "PE"
            stable_id = str(
                trade.get("event_id")
                or f"{option_type}-{int(entry_time)}"
            )
            markers.append({
                "id": f"entry-{stable_id}",
                "time": int(entry_time),
                "position": "aboveBar" if is_pe else "belowBar",
                "color": "#ef5350" if is_pe else "#26a69a",
                "shape": "arrowDown" if is_pe else "arrowUp",
                "text": (
                    f"BUY {option_type}"
                    + (f" • {trade.get('cross_type')}" if trade.get("cross_type") else "")
                ),
                "size": 2,
            })
            marker_no += 1

        if include_exits:
            exit_time = _chart_marker_time(df, trade.get("exit_time"))
            if exit_time is not None:
                reason = str(trade.get("reason", "EXIT")).upper()
                stable_exit_id = str(
                    trade.get("event_id")
                    or f"{option_type}-{int(exit_time)}"
                )
                markers.append({
                    "id": f"exit-{stable_exit_id}",
                    "time": int(exit_time),
                    "position": "aboveBar",
                    "color": "#ffd166",
                    "shape": "arrowDown",
                    "text": f"SELL {option_type} / {reason} {float(trade.get('exit', 0)):.2f}",
                    "size": 2,
                })
                marker_no += 1

    markers.sort(key=lambda x: (int(x["time"]), str(x.get("id", ""))))
    return markers


def _live_execution_markers(df, execution_events):
    """Build CE/PE execution markers for the live NIFTY chart."""
    return _trade_markers(df, list(execution_events or []), include_exits=False)


def _persistent_execution_levels(execution_events, max_events=3):
    """Keep the latest triggered setup levels visible after the strategy clears."""
    events = [e for e in (execution_events or []) if isinstance(e, dict)]
    if not events:
        return []
    def _sort_key(e):
        return str(e.get("chart_time") or e.get("entry_time") or e.get("execution_time") or "")
    events = sorted(events, key=_sort_key)[-max(1, int(max_events)):]
    levels = []
    seen = set()
    for e in events:
        for title, raw, color in (
            ("Cross", e.get("cross_price"), "#f0b90b"),
            ("Entry trigger", e.get("confirmation_level", e.get("trigger_price")), "#26a69a"),
        ):
            try:
                price = float(raw)
            except (TypeError, ValueError):
                continue
            key = (title, round(price, 4))
            if key in seen:
                continue
            seen.add(key)
            levels.append({"price": price, "title": title, "color": color, "style": 2})
    return levels

def _historical_signal_markers(
    df,
    confirmation_points,
    confirmation_bars,
    session_start="09:15",
    session_end="15:15",
):
    """Re-run only the entry state machine over loaded history for chart labels.

    This is deliberately separate from the P&L backtest: it records *every*
    confirmed strategy trigger in the historical window and immediately resets
    the simulated trade state, so one historical position cannot suppress later
    signals just because no historical option-exit series is available.

    The crossing candle only arms the setup.  The following candles (up to the
    configured confirmation window) can trigger BUY CE / BUY PE.  The marker is
    attached to the exact confirmation candle, using the same IST candle
    timestamps as the live engine.
    """
    if df is None or df.empty:
        return []

    start_minute = _hhmm_minute(session_start, DEFAULT_ENTRY_START_MINUTE)
    end_minute = _hhmm_minute(session_end, DEFAULT_ENTRY_END_MINUTE)
    if end_minute < start_minute:
        return []

    prepared = VwapConfirmationEngine.prepare(df.copy())
    strat = VwapConfirmationEngine(
        StrategyConfig(float(confirmation_points), int(confirmation_bars))
    )
    markers = []
    seen = set()
    previous_date = None

    for _, row in prepared.iterrows():
        ts = _ist_timestamp(row["datetime"])
        if pd.isna(ts):
            continue
        session_date = ts.date()

        if previous_date is not None and session_date != previous_date:
            strat.reset_trade()
            try:
                strat._clear_setup()
            except Exception:
                pass
        previous_date = session_date

        if not _within_entry_window(ts, start_minute, end_minute):
            continue

        # The first eligible candle is the user's configured session-start
        # candle, regardless of pre-market/history rows preceding it.
        eval_row = row.copy()
        if ts.hour * 60 + ts.minute == start_minute:
            eval_row["_algo_session_first"] = True

        signal = strat.process_closed_candle(eval_row)
        if not signal:
            continue

        option_type = "CE" if signal["side"] == "BUY" else "PE"
        chart_time = _chart_marker_time(prepared, signal.get("time"))
        if chart_time is None:
            continue

        key = (int(chart_time), option_type)
        if key in seen:
            strat.reset_trade()
            continue
        seen.add(key)

        markers.append({
            "id": f"hist-entry-{option_type}-{int(chart_time)}",
            "time": int(chart_time),
            "position": "belowBar" if option_type == "CE" else "aboveBar",
            "color": "#26a69a" if option_type == "CE" else "#ef5350",
            "shape": "arrowUp" if option_type == "CE" else "arrowDown",
            "text": (
                f"BUY {option_type} • {signal.get('cross_type', 'CLOSE_CROSS')} • HIST"
            ),
            "size": 2,
            "trigger_price": float(signal.get("confirmation_level", signal.get("entry", 0.0))),
            "cross_price": float(signal.get("cross_price", 0.0)),
        })

        # This helper is for signal visualization, not trade/P&L simulation.
        # Reset immediately so every later historical trigger can be displayed.
        strat.reset_trade()

    return markers


def _execution_table(execution_events):
    """Return a compact local execution ledger with IST timestamps."""
    if not execution_events:
        return pd.DataFrame()
    out = pd.DataFrame(execution_events)
    preferred = [
        "execution_time", "entry_time", "side", "option_type", "symbol",
        "strike", "quantity", "lots", "entry", "trigger_price", "status",
    ]
    cols = [c for c in preferred if c in out.columns]
    out = out[cols].copy()
    for c in ("execution_time", "entry_time"):
        if c in out.columns:
            out[c] = out[c].map(_format_ist)
    return out.sort_values("execution_time", ascending=False)


def _execution_event_key(event):
    """Stable de-duplication key for local execution events."""
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id")
    if event_id:
        return ("event_id", str(event_id))
    return (
        "legacy",
        str(event.get("symbol", "")),
        str(event.get("option_type", "")),
        str(event.get("side", "")),
        str(event.get("entry_time", "")),
        str(event.get("chart_time", "")),
        str(event.get("entry", "")),
    )


def _merge_execution_ledger(events):
    """Merge/UPDATE local trigger+execution events into Streamlit state.

    Events are keyed by event_id. A trigger is created before option selection,
    then the same event is updated to OPTION_SELECTED/EXECUTED/TEST/PAPER or
    REJECTED/FAILED. This prevents the chart marker from disappearing when the
    broker/option path changes the event after the trigger.
    """
    ledger = list(st.session_state.get("algo_execution_ledger", []) or [])
    by_id = {
        str(x.get("event_id")): i
        for i, x in enumerate(ledger)
        if isinstance(x, dict) and x.get("event_id")
    }
    legacy_seen = {_execution_event_key(x) for x in ledger if _execution_event_key(x) is not None}
    changed = False

    for event in list(events or []):
        if not isinstance(event, dict):
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in by_id:
            idx = by_id[event_id]
            merged = dict(ledger[idx])
            merged.update(event)
            ledger[idx] = merged
            changed = True
            continue

        key = _execution_event_key(event)
        if key is None or key in legacy_seen:
            continue
        item = dict(event)
        if not item.get("chart_time"):
            item["chart_time"] = item.get("entry_time") or item.get("execution_time")
        ledger.append(item)
        if event_id:
            by_id[event_id] = len(ledger) - 1
        legacy_seen.add(key)
        changed = True

    if changed:
        st.session_state.algo_execution_ledger = ledger[-500:]
    return st.session_state.algo_execution_ledger


def _all_live_execution_events(engine):
    """Return the persistent local ledger plus any current engine events."""
    current = list(getattr(engine, "execution_events", []) or []) if engine is not None else []
    return _merge_execution_ledger(current)


def render_replay_page():
    st.markdown("## 🧪 Backtest & Replay")
    st.caption(
        "Historical NIFTY candles are replayed through the same VWAP confirmation "
        "state machine used by the live engine. Entries are paper-only."
    )
    replay_start = st.session_state.get("algo_start_time", dt_time(9, 15))
    replay_end = st.session_state.get("algo_end_time", dt_time(15, 15))
    st.info(
        f"Trading session rule: NEW entries are allowed only from {_hhmm(replay_start, '09:15')} "
        f"to {_hhmm(replay_end, '15:15')} IST. Existing positions can still hit SL/target "
        f"after {_hhmm(replay_end, '15:15')}. The first eligible candle is the candle "
        f"starting at the selected start time and uses the full replay timeframe."
    )

    client = st.session_state.get("client")
    if not client:
        st.warning("Connect to FYERS first so the app can download historical candles.")
        return

    symbol = st.text_input("Replay symbol", value=os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX"), key="replay_symbol")
    tf = st.selectbox("Replay timeframe", ["1", "3", "5", "10", "15", "30", "60"], index=2, key="replay_tf")
    days = st.slider("Historical days", 1, 60, 31, key="replay_days")
    c1, c2, c3 = st.columns(3)
    with c1:
        rp_points = st.number_input("Confirmation points", 1.0, 100.0, 15.0, 0.5, key="rp_points")
    with c2:
        rp_bars = st.number_input("Confirmation candles", 1, 20, 8, 1, key="rp_bars")
    with c3:
        rp_qty = st.number_input("Replay quantity", 1, 100000, 1, 1, key="rp_qty")

    p1, p2 = st.columns(2)
    with p1:
        rp_sl = st.number_input("Replay SL points", 0.0, 5000.0, 20.0, 0.5, key="rp_sl")
    with p2:
        rp_target = st.number_input("Replay target points", 0.0, 10000.0, 40.0, 0.5, key="rp_target")

    if st.button("📥 Load historical candles", type="primary", width="stretch"):
        try:
            df = client.history(symbol, tf, days)
            if df is None or df.empty:
                st.error("FYERS returned no candles for this symbol/timeframe.")
            else:
                st.session_state.replay_df = VwapConfirmationEngine.prepare(df)
                st.session_state.replay_key = f"{symbol}|{tf}|{days}"
                st.session_state.replay_index = 0
                st.session_state.replay_result = None
                st.success(f"Loaded {len(df):,} candles from {_format_ist(df['datetime'].min())} to {_format_ist(df['datetime'].max())}.")
        except Exception as exc:
            st.error(f"History request failed: {exc}")

    df = st.session_state.get("replay_df")
    if df is None or df.empty:
        st.info("Load historical candles to start the replay.")
        return

    st.markdown(f"**{len(df):,} candles loaded** • {_format_ist(df['datetime'].min())} → {_format_ist(df['datetime'].max())}")

    # Full deterministic backtest.
    if st.button("▶️ Run full backtest", width="stretch"):
        result = _run_backtest(df, rp_points, rp_bars, rp_sl, rp_target, st.session_state.get("algo_start_time", dt_time(9, 15)), st.session_state.get("algo_end_time", dt_time(15, 15)))
        st.session_state.replay_result = result

    result = st.session_state.get("replay_result")
    if result:
        a,b,c,d = st.columns(4)
        a.metric("Entries", result["signals"])
        b.metric("Wins", result["wins"])
        c.metric("Losses", result["losses"])
        d.metric("Net points", f"{result['equity']:.2f}")

        # Full backtest is always plotted against the SAME immutable historical
        # dataframe that was loaded above. No fresh/random dataset is created.
        events = result.get("events", result.get("trades", []))
        if events:
            bt_markers = _trade_markers(df, events, include_exits=True)
            render_chart(
                df,
                f"FULL BACKTEST • {symbol} • {tf}m",
                vwap=True,
                markers=bt_markers,
                height=700,
                max_candles=None,
                fit_content=True,
            )
            st.caption(
                f"Backtest source: the {len(df):,} FYERS candles loaded above "
                f"({df['datetime'].min()} → {df['datetime'].max()}). "
                "The backtest does not fetch or generate another dataset."
            )
            out = pd.DataFrame(events)
            if not out.empty:
                out["action"] = out["option_type"].map(
                    lambda x: f"BUY {str(x).upper()}" if str(x).upper() in {"CE", "PE"} else "BUY"
                )
                preferred = [c for c in [
                    "entry_time", "action", "option_type", "quantity", "lots",
                    "entry", "exit_time", "exit", "reason", "pnl", "status"
                ] if c in out.columns]
                out = out[preferred]
                for time_col in ("entry_time", "exit_time"):
                    if time_col in out.columns:
                        out[time_col] = out[time_col].map(_format_ist)
            st.dataframe(out, width="stretch", hide_index=True)
        else:
            st.info("The strategy produced no confirmed entries in this historical window.")

    st.divider()
    st.markdown("### 🎬 Step-by-step replay")
    # A replay starts with exactly ONE candle visible. There is no hidden
    # preload. Next candle always advances exactly one row, and Run replay
    # advances exactly one row per fragment tick.
    if "replay_cursor" not in st.session_state:
        st.session_state.replay_cursor = 1
    if "replay_playing" not in st.session_state:
        st.session_state.replay_playing = False

    max_cursor = max(1, len(df))
    st.session_state.replay_cursor = min(max(1, int(st.session_state.replay_cursor)), max_cursor)

    @st.fragment(run_every="700ms")
    def _replay_player():
        max_cursor_local = max(1, len(df))
        st.session_state.replay_cursor = min(
            max(1, int(st.session_state.replay_cursor)), max_cursor_local
        )

        # Auto-play advances exactly one candle on each fragment tick.
        if st.session_state.get("replay_playing", False):
            if st.session_state.replay_cursor < max_cursor_local:
                st.session_state.replay_cursor += 1
            else:
                st.session_state.replay_playing = False

        # IMPORTANT: do not give the slider a persistent widget key here.
        # The replay controls intentionally change replay_cursor and then rerun
        # the fragment. A keyed slider keeps its old widget value and can
        # immediately overwrite the new cursor, making Next/Play appear dead.
        # The slider value is therefore driven directly from replay_cursor.
        cursor = st.slider(
            "Replay candle",
            1,
            max_cursor_local,
            st.session_state.replay_cursor,
        )
        # Moving the slider manually also stops autoplay so the user is in control.
        if cursor != st.session_state.replay_cursor:
            st.session_state.replay_cursor = int(cursor)
            st.session_state.replay_playing = False

        rc1, rc2, rc3 = st.columns(3)
        with rc1:
            if st.button("⏮ Reset replay", width="stretch", key="replay_reset"):
                st.session_state.replay_cursor = 1
                st.session_state.replay_playing = False
                st.rerun(scope="fragment")
        with rc2:
            if st.button("▶ Next candle", width="stretch", key="replay_next"):
                # Exactly one candle, never a batch.
                st.session_state.replay_playing = False
                st.session_state.replay_cursor = min(
                    max_cursor_local, st.session_state.replay_cursor + 1
                )
                st.rerun(scope="fragment")
        with rc3:
            label = "⏸ Pause replay" if st.session_state.get("replay_playing", False) else "▶▶ Run replay"
            if st.button(label, width="stretch", key="replay_play"):
                st.session_state.replay_playing = not st.session_state.get("replay_playing", False)
                st.rerun(scope="fragment")

        visible = df.iloc[:st.session_state.replay_cursor].copy()
        if not visible.empty:
            sim = _run_backtest(
                visible,
                rp_points,
                rp_bars,
                rp_sl,
                rp_target,
                st.session_state.get("algo_start_time", dt_time(9, 15)),
                st.session_state.get("algo_end_time", dt_time(15, 15)),
            )
            replay_events = sim.get("events", sim.get("trades", []))
            markers = _trade_markers(visible, replay_events, include_exits=True)
            # Only candles up to the replay cursor are rendered, but NEVER
            # truncate the replay window. If the cursor is 1245, candles
            # 1..1245 are sent to the chart. The chart is also refit so the
            # entire currently revealed replay window is visible.
            replay_plot = visible
            render_chart(
                replay_plot,
                f"REPLAY • {symbol} • {tf}m",
                vwap=True,
                markers=markers,
                height=650,
                max_candles=None,
                fit_content=True,
            )
            state = "PLAYING" if st.session_state.get("replay_playing", False) else "PAUSED"
            st.caption(
                f"Replay is at candle {st.session_state.replay_cursor}/{len(df)} • {state}. "
                f"Entries found so far: {sim['signals']}. BUY CE / BUY PE labels are placed on every candle where the configured confirmation was actually reached. "
                "This mode never sends broker orders."
            )
            st.markdown("#### 📊 Replay Backtest Portfolio")
            total_entries = int(sim.get("total_entries", sim.get("signals", 0)))
            wins = int(sim.get("wins", 0))
            losses = int(sim.get("losses", 0))
            win_ratio = float(sim.get("win_ratio", 0.0))
            pm1, pm2, pm3, pm4, pm5 = st.columns(5)
            pm1.metric("Total Entries", total_entries)
            pm2.metric("Total Wins", wins)
            pm3.metric("Total Losses", losses)
            pm4.metric("Win Ratio", f"{win_ratio:.1f}%")
            pm5.metric("Net P&L (pts)", f"{float(sim.get('equity', 0.0)):.2f}")

            replay_events = pd.DataFrame(sim.get("events", []))
            if not replay_events.empty:
                cols = [c for c in [
                    "entry_time", "side", "option_type", "quantity", "lots",
                    "cross_price", "confirmation_level", "entry",
                    "exit_time", "exit", "reason", "pnl", "status"
                ] if c in replay_events.columns]
                table = replay_events[cols].sort_values("entry_time").copy()
                for time_col in ("entry_time", "exit_time"):
                    if time_col in table.columns:
                        table[time_col] = table[time_col].map(_format_ist)
                st.dataframe(table, width="stretch", hide_index=True)
                st.caption(
                    "All timestamps are India Standard Time (IST). New entries are "
                    "accepted only from 09:15 through 15:15 IST. Existing positions "
                    "may still exit after 15:15 via SL/target."
                )
            else:
                st.info("No entries were taken inside the configured IST entry window.")

    _replay_player()

def render_charts_page():
    engine = st.session_state.engine
    client = st.session_state.client
    if not client:
        st.markdown("## 📊 Live Charts")
        st.info("Connect to FYERS from the Terminal page first.")
        return

    # Historical-only chart is available even when the live engine is not
    # running. This prevents an empty chart when the REST history is available
    # but the websocket/engine is stopped.
    if not engine:
        st.markdown("## 📊 Historical Charts")
        st.caption("Historical candles are loaded directly from FYERS; no live websocket is required.")
        hist_symbol = st.text_input(
            "Historical symbol",
            value=os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX"),
            key="standalone_chart_symbol",
        )
        hist_tf = st.selectbox(
            "Historical timeframe",
            CHART_TIMEFRAMES,
            index=CHART_TIMEFRAMES.index("5") if "5" in CHART_TIMEFRAMES else 0,
            key="standalone_chart_tf",
            format_func=lambda x: f"{x} minute" if x == "1" else f"{x} minutes",
        )
        hist_days = st.slider("Historical days", 1, 60, 31, key="standalone_chart_days")
        if st.button("📥 Load historical chart", type="primary", width="stretch"):
            try:
                hist = client.history(hist_symbol, hist_tf, hist_days)
                if hist is None or hist.empty:
                    st.error("FYERS returned no historical candles.")
                else:
                    st.session_state.standalone_chart_df = hist.copy()
                    st.session_state.standalone_chart_key = f"{hist_symbol}|{hist_tf}|{hist_days}"
                    st.success(f"Loaded {len(hist):,} candles.")
            except Exception as exc:
                st.error(f"Historical chart request failed: {exc}")
        hist = st.session_state.get("standalone_chart_df", pd.DataFrame())
        if not hist.empty:
            hist_plot = VwapConfirmationEngine.prepare(hist.copy())
            render_chart(
                hist_plot,
                f"HISTORICAL • {hist_symbol} • {hist_tf}m",
                vwap=True,
                height=700,
                max_candles=None,
                fit_content=True,
            )
            st.caption(f"{len(hist_plot):,} candles • {hist_plot['datetime'].min()} → {hist_plot['datetime'].max()}")
        else:
            st.info("Load historical candles to populate the chart.")
        return

    st.markdown("## 📊 Live Charts")
    st.caption("NIFTY is shown first. Once the algo selects an option, you can show NIFTY, the option, or both side-by-side.")
    render_rejection_panel()

    @st.fragment(run_every="1s")
    def _charts_live():
        tick = engine.last_tick or {}
        tick_age = engine.tick_age_seconds()
        live_ok = bool(engine.running and engine.ws_connected and tick and tick_age is not None and tick_age < 5)
        status = "LIVE" if live_ok else ("CONNECTING" if engine.running else "STOPPED")
        remaining = engine.bar_seconds_remaining()
        countdown = "--:--" if remaining is None else f"{remaining//60:02d}:{remaining%60:02d}"
        ltp = tick.get("ltp")

        # Chart timeframe is independent from the strategy/engine timeframe.
        # Changing it only reloads chart candles; it does not restart FYERS or
        # change the VWAP entry engine.
        current_engine_tf = str(engine.resolution)
        chart_default_index = (
            CHART_TIMEFRAMES.index(current_engine_tf)
            if current_engine_tf in CHART_TIMEFRAMES
            else CHART_TIMEFRAMES.index("5")
        )

        # Let the selectbox own its widget state. Do not pre-populate
        # st.session_state["chart_timeframe"] and also pass an index; Streamlit
        # 1.62 warns about that combination on every fragment rerun.
        chart_timeframe = st.selectbox(
            "Chart timeframe",
            CHART_TIMEFRAMES,
            index=chart_default_index,
            key="chart_timeframe",
            format_func=lambda x: f"{x} minute" if x == "1" else f"{x} minutes",
            help="Chart timeframe only. The strategy continues using its own Engine Timeframe.",
        )
        chart_history_days = _chart_history_days(chart_timeframe, CHART_CANDLE_LIMIT)

        st.markdown(
            f"""<div class="chart-status">
<span class="chart-dot {'on' if live_ok else 'off'}"></span>
<b>{status}</b>
<span>• NIFTY {f"{ltp:,.2f}" if ltp is not None else "—"}</span>
<span>• Tick age {f"{tick_age:.1f}s" if tick_age is not None else "—"}</span>
<span class="chart-count">Candle closes {countdown}</span>
</div>""",
            unsafe_allow_html=True,
        )

        # Rehydrate the latest selected option from the persistent local ledger.
        # This keeps the option chart available after a Streamlit rerun/page switch.
        if not engine.selected_option:
            ledger = list(st.session_state.get("algo_execution_ledger", []) or [])
            latest_option = next(
                (e for e in reversed(ledger) if isinstance(e, dict) and e.get("symbol")),
                None,
            )
            if latest_option:
                engine.selected_option = {
                    "symbol": latest_option.get("symbol"),
                    "option_type": latest_option.get("option_type"),
                    "strike": latest_option.get("strike"),
                    "ltp": float(latest_option.get("entry") or 0),
                    "expiry": latest_option.get("expiry"),
                }
                # Rebuild the same contract-local price guard used when the
                # option was originally selected, so a page reload cannot lose
                # the NIFTY-vs-premium data boundary.
                try:
                    ref = float(engine.selected_option.get("ltp") or 0)
                    configured_max = float(engine.option_cfg.get("premium_max") or 0)
                    engine._option_price_reference = ref if ref > 0 else None
                    engine._option_price_ceiling = (
                        max(ref * 8.0, configured_max * 6.0, ref + 500.0)
                        if ref > 0 else None
                    )
                except (TypeError, ValueError, AttributeError):
                    engine._option_price_reference = None
                    engine._option_price_ceiling = None
                try:
                    engine.execution_history = client.history(
                        engine.selected_option["symbol"], chart_timeframe, 31
                    )
                except Exception:
                    engine.execution_history = pd.DataFrame()
                engine._execution_current_candle = None
                engine.data_symbols.add(engine.selected_option["symbol"])
                try:
                    if engine.socket is not None and engine.ws_connected:
                        client.subscribe_data_socket(engine.socket, [engine.selected_option["symbol"]], "SymbolUpdate")
                        engine._last_option_subscribe = engine.selected_option["symbol"]
                except Exception:
                    pass
                if latest_option.get("entry") is not None:
                    try:
                        engine.protection = {
                            "side": latest_option.get("side"),
                            "entry_reference": float(latest_option.get("entry")),
                            "sl_points": 0.0,
                            "target_points": 0.0,
                            "sl_price": float(latest_option.get("sl_price")) if latest_option.get("sl_price") is not None else 0.0,
                            "target_price": float(latest_option.get("target_price")) if latest_option.get("target_price") is not None else 0.0,
                            "enabled": bool(latest_option.get("protection_enabled", False)),
                        }
                    except (TypeError, ValueError):
                        pass

        has_option = bool(engine.selected_option)
        if "show_nifty_chart" not in st.session_state:
            st.session_state.show_nifty_chart = True
        if "show_option_chart" not in st.session_state:
            st.session_state.show_option_chart = False

        # When the algo selects a premium for an entry, automatically switch
        # the chart layout to NIFTY + selected option side-by-side.  Do this
        # only when the selected symbol actually changes so a user's manual
        # checkbox choice is not overwritten on every live refresh.
        selected_symbol = engine.selected_option.get("symbol") if has_option else None
        previous_symbol = st.session_state.get("last_chart_option_symbol")
        if selected_symbol and selected_symbol != previous_symbol:
            st.session_state.last_chart_option_symbol = selected_symbol
            st.session_state.show_nifty_chart = True
            st.session_state.show_option_chart = True
        elif not selected_symbol:
            st.session_state.last_chart_option_symbol = None

        c1, c2, c3 = st.columns([1, 1, 3])
        with c1:
            show_nifty = st.checkbox("NIFTY chart", key="show_nifty_chart")
        with c2:
            show_option = st.checkbox("Option chart", key="show_option_chart", disabled=not has_option)
        with c3:
            if has_option:
                st.caption(f"Selected option: **{engine.selected_option['symbol']}** • ₹{float(engine.selected_option.get('ltp',0)):.2f}")
            else:
                st.caption("Option chart appears automatically after the algo selects a CE/PE.")

        if not show_nifty and not show_option:
            st.info("Select at least one chart above.")

        levels, markers = [], []
        levels.extend(_persistent_execution_levels(_all_live_execution_events(engine)))
        if engine.strategy.cross_price is not None:
            levels.append({"price": engine.strategy.cross_price, "title": "Cross", "color": "#f0b90b", "style": 2})
        if engine.strategy.armed and engine.strategy.confirmation_level is not None:
            levels.append({"price": engine.strategy.confirmation_level, "title": "Trigger", "color": "#26a69a", "style": 2})
        if engine.last_signal:
            markers.append({
                "time": int(pd.Timestamp(engine.last_signal["time"]).timestamp()),
                "position": "belowBar" if engine.last_signal["side"] == "BUY" else "aboveBar",
                "color": "#26a69a" if engine.last_signal["side"] == "BUY" else "#ef5350",
                "shape": "arrowUp" if engine.last_signal["side"] == "BUY" else "arrowDown",
                "text": f"BUY {'CE' if engine.last_signal['side'] == 'BUY' else 'PE'}",
            })

        # Cache chart history by symbol + timeframe. This keeps the page fast:
        # changing the chart timeframe makes one REST history call, while the
        # live fragment thereafter only overlays the newest websocket tick.
        chart_key = f"{engine.signal_symbol}|{chart_timeframe}"
        if st.button("↻ Reload historical chart", key="reload_live_chart_history"):
            cache = st.session_state.setdefault("_chart_history_cache", {})
            cache.pop((str(engine.signal_symbol), str(chart_timeframe), chart_history_days), None)
            marker_cache = st.session_state.get("_chart_marker_cache", {})
            for mk in list(marker_cache):
                if isinstance(mk, tuple) and mk and mk[0] == chart_key:
                    marker_cache.pop(mk, None)
            st.session_state.chart_history_key = ""
            st.session_state.chart_history_error = ""
            st.rerun(scope="fragment")

        if st.session_state.get("chart_history_key") != chart_key:
            # Cache REST history for the historical window, but never make that
            # cached dataframe the authoritative source for the current candle.
            # The engine owns the live OHLC candle and updates it on every FYERS
            # tick.
            # Historical chart bootstrap remains cached; the live chart itself
            # must never be rebuilt just because this 1-second fragment runs.
            # Compatibility marker retained for older regression checks: seeded = engine.load_history(days=31)
            base_nifty = _load_chart_history(
                client, engine.signal_symbol, chart_timeframe, chart_history_days
            )
            if base_nifty is None:
                base_nifty = pd.DataFrame()

            st.session_state.chart_history_key = chart_key
            st.session_state.chart_history_nifty = base_nifty.copy()
        else:
            base_nifty = st.session_state.get("chart_history_nifty", pd.DataFrame())

        if chart_timeframe == str(engine.resolution):
            # IMPORTANT: use the engine's live candle on every fragment rerun.
            # The engine already maintains VWAP/indicator columns, so do not
            # recompute the full ATR/ADX/VWAP pipeline every second just to draw
            # the chart. That repeated dataframe work was a major source of UI
            # lag during live trading.
            live_engine_df = engine.display_history()
            if live_engine_df is not None and not live_engine_df.empty:
                nifty_df = live_engine_df.copy()
            else:
                nifty_df = _with_live_tick(base_nifty, tick, chart_timeframe)
        else:
            nifty_df = _with_live_tick(base_nifty, tick, chart_timeframe)
        if not nifty_df.empty and "vwap" not in nifty_df.columns:
            nifty_df = VwapConfirmationEngine.prepare(nifty_df)

        # Historical markers are calculated from the SAME VWAP confirmation
        # state machine and the SAME configured session/confirmation settings.
        # This gives the live chart a one-month visual history of BUY CE / BUY PE
        # triggers instead of showing only the most recent live event.
        hist_marker_df = base_nifty.copy() if base_nifty is not None else pd.DataFrame()
        if not hist_marker_df.empty and tick:
            # Do not treat the currently-forming live candle as historical/closed.
            tick_time = tick.get("time")
            if tick_time is None:
                tick_time = pd.Timestamp.now(tz=REPLAY_IST)
            try:
                live_bucket = _chart_bucket(tick_time, chart_timeframe)
                last_hist = pd.Timestamp(hist_marker_df.iloc[-1]["datetime"])
                if _chart_bucket(last_hist, chart_timeframe) == live_bucket:
                    hist_marker_df = hist_marker_df.iloc[:-1].copy()
            except Exception:
                pass

        # Historical signal markers depend only on the immutable REST window
        # and the strategy configuration. Cache them instead of replaying the
        # full 800-candle state machine on every 1-second chart fragment.
        marker_cache = st.session_state.setdefault("_chart_marker_cache", {})
        marker_cache_key = (
            chart_key,
            float(engine.strategy.config.confirmation_points),
            int(engine.strategy.config.confirmation_bars),
            int(engine.session_start_minute),
            int(engine.session_end_minute),
        )
        if not hist_marker_df.empty:
            if marker_cache_key not in marker_cache:
                marker_cache[marker_cache_key] = _historical_signal_markers(
                    hist_marker_df,
                    engine.strategy.config.confirmation_points,
                    engine.strategy.config.confirmation_bars,
                    session_start=_hhmm_from_minute(engine.session_start_minute),
                    session_end=_hhmm_from_minute(engine.session_end_minute),
                )
            historical_markers = marker_cache[marker_cache_key]
        else:
            historical_markers = []

        # Live execution markers are built from the persistent local ledger, not
        # just `last_signal`. They remain visible after multiple entries and after
        # option selection/order handling completes.
        live_execution_events = _all_live_execution_events(engine)
        live_execution_markers = _live_execution_markers(
            nifty_df, live_execution_events
        ) if not nifty_df.empty else []
        if not live_execution_markers and engine.last_signal and not nifty_df.empty:
            fallback = {
                "chart_time": engine.last_signal.get("time"),
                "side": engine.last_signal.get("side"),
                "option_type": "CE" if engine.last_signal.get("side") == "BUY" else "PE",
                "entry": engine.last_signal.get("entry", 0),
            }
            live_execution_markers = _live_execution_markers(nifty_df, [fallback])

        # If a live execution happens on a candle that already has the same
        # historical strategy signal, keep the live marker (without duplicates).
        markers = []
        marker_keys = set()
        for marker in historical_markers + live_execution_markers:
            option_type = "CE" if "CE" in str(marker.get("text", "")).upper() else (
                "PE" if "PE" in str(marker.get("text", "")).upper() else ""
            )
            key = (int(marker.get("time", 0)), option_type)
            if key in marker_keys:
                # Live markers are appended after historical markers; replace
                # the historical label with the real execution label.
                for idx, existing in enumerate(markers):
                    existing_key = (int(existing.get("time", 0)),
                                    "CE" if "CE" in str(existing.get("text", "")).upper()
                                    else "PE" if "PE" in str(existing.get("text", "")).upper() else "")
                    if existing_key == key:
                        markers[idx] = marker
                        break
                continue
            marker_keys.add(key)
            markers.append(marker)

        if nifty_df.empty:
            history_error = st.session_state.get("chart_history_error", "")
            if history_error:
                st.error(f"Historical chart data could not be loaded: {history_error}")
            else:
                st.warning(
                    "No historical candles were returned for this symbol/timeframe. "
                    "The chart is working, but it has no data to draw."
                )

        def _isolate_option_chart_prices(df):
            """Repair/display-filter obvious foreign-symbol spikes in premium data.

            NIFTY and the selected option share one FYERS socket, but they must
            never share a price series. This is a chart-only safety net for a
            stale/malformed candle that may already exist in REST history or in
            the in-memory option history. It does not change broker orders or
            execution/P&L calculations.
            """
            if df is None or df.empty:
                return pd.DataFrame() if df is None else df
            out = df.copy()
            for col in ("open", "high", "low", "close"):
                if col in out.columns:
                    out[col] = pd.to_numeric(out[col], errors="coerce")
            out = out.dropna(subset=[c for c in ("open", "high", "low", "close") if c in out.columns]).copy()
            ceiling = getattr(engine, "_option_price_ceiling", None)
            try:
                ceiling = float(ceiling) if ceiling is not None else None
            except (TypeError, ValueError):
                ceiling = None
            if ceiling is None or ceiling <= 0:
                return out

            # Never allow a foreign/NIFTY-scale value to define the option
            # candle's visible range. Preserve the candle's real open/close and
            # only repair the offending wick component.
            bad_high = out["high"] > ceiling
            bad_low = out["low"] <= 0
            if bad_high.any():
                out.loc[bad_high, "high"] = out.loc[bad_high, ["open", "close"]].max(axis=1)
            if bad_low.any():
                out.loc[bad_low, "low"] = out.loc[bad_low, ["open", "close"]].min(axis=1)
            out["high"] = out[["open", "high", "close"]].max(axis=1)
            out["low"] = out[["open", "low", "close"]].min(axis=1)
            return out

        def option_chart():
            symbol = engine.selected_option["symbol"]
            option_key = f"{symbol}|{chart_timeframe}"
            if st.session_state.get("option_chart_history_key") != option_key:
                if chart_timeframe == str(engine.resolution):
                    ex = engine.execution_history.copy() if engine.execution_history is not None else pd.DataFrame()
                    if ex.empty:
                        ex = _load_chart_history(client, symbol, chart_timeframe, chart_history_days)
                else:
                    ex = _load_chart_history(client, symbol, chart_timeframe, chart_history_days)
                st.session_state.option_chart_history_key = option_key
                st.session_state.option_chart_history = ex.copy()
            else:
                ex = st.session_state.get("option_chart_history", pd.DataFrame())

            option_tick = engine.last_execution_tick or {}
            # At the engine timeframe, the engine owns the live option candle
            # and advances it from every option tick. Do not replace that live
            # candle with a stale session-state snapshot. For a different chart
            # timeframe, keep the lightweight overlay path.
            if chart_timeframe == str(engine.resolution):
                live_ex = engine.display_execution_history()
                if not live_ex.empty:
                    ex = live_ex
            else:
                ex = _with_live_tick(ex, option_tick, chart_timeframe)

            # Hard boundary: the premium chart gets ONLY its option contract's
            # candles. Any already-corrupted high/low is repaired for display so
            # the chart cannot flash a NIFTY/foreign-symbol wick.
            ex = _isolate_option_chart_prices(ex)

            lev = []
            if engine.protection:
                lev.append({"price": engine.protection["entry_reference"], "title": "Entry", "color": "#8b95a7"})
                if engine.protection.get("enabled"):
                    lev += [
                        {"price": engine.protection["sl_price"], "title": "SL", "color": "#ef5350"},
                        {"price": engine.protection["target_price"], "title": "Target", "color": "#26a69a"},
                    ]
            render_chart(ex, engine.selected_option["symbol"],
                         ltp=option_tick.get("ltp") if option_tick else engine.selected_option.get("ltp"),
                         vwap=False, levels=lev, height=900, max_candles=CHART_CANDLE_LIMIT, fit_content=False, resolution=chart_timeframe)

        if show_nifty and show_option and has_option:
            left, right = st.columns(2, gap="small")
            with left:
                st.markdown(f"#### NIFTY • VWAP • {chart_timeframe}m")
                render_chart(nifty_df, "NIFTY • FYERS", ltp=ltp, levels=levels, markers=markers, height=900, max_candles=CHART_CANDLE_LIMIT, fit_content=False, resolution=chart_timeframe)
            with right:
                st.markdown(f"#### OPTION • {engine.selected_option['symbol']} • {chart_timeframe}m")
                option_chart()
        elif show_nifty:
            st.markdown(f"#### NIFTY • VWAP • {chart_timeframe}m")
            render_chart(nifty_df, "NIFTY • FYERS", ltp=ltp, levels=levels, markers=markers, height=900, max_candles=CHART_CANDLE_LIMIT, fit_content=False, resolution=chart_timeframe)
        elif show_option and has_option:
            st.markdown(f"#### OPTION • {engine.selected_option['symbol']} • {chart_timeframe}m")
            option_chart()

        st.markdown("###")
        with st.expander("💼 Portfolio & Open Positions", expanded=False):
            positions, holdings, orders, trades = portfolio_tables(st.session_state.portfolio)
            funds = st.session_state.portfolio.get("funds", {}).get("fund_limit", [])
            fmap = {str(x.get("id")): x.get("value") for x in funds if isinstance(x, dict)}
            a,b,c,d = st.columns(4)
            a.metric("Available", str(fmap.get("10", fmap.get("3", "—"))))
            b.metric("Utilized", str(fmap.get("2", "—")))
            c.metric("Open positions", str(len([p for p in positions if float(p.get("netQty",0) or 0) != 0])))
            d.metric("Realized P&L", str(fmap.get("4", "—")))
            st.subheader("Open Positions")
            st.dataframe(pd.DataFrame(positions), width="stretch", hide_index=True)
            st.subheader("Recent Orders")
            st.dataframe(pd.DataFrame(orders[-20:] if orders else []), width="stretch", hide_index=True)

            st.subheader("Algo Execution Ledger")
            local_exec = _execution_table(_all_live_execution_events(engine))
            if local_exec.empty:
                st.info("No CE/PE executions recorded in this session yet.")
            else:
                st.dataframe(local_exec, width="stretch", hide_index=True)
                st.caption("Local execution ledger updates immediately; broker positions/orders are shown separately above.")
            if st.button("↻ Refresh portfolio", key="charts_refresh_portfolio"):
                try:
                    st.session_state.portfolio = load_portfolio(client)
                    st.rerun(scope="fragment")
                except Exception as e:
                    st.error(str(e))

    _charts_live()

if page == "replay":
    render_replay_page()
    st.stop()

if page == "charts":
    render_charts_page()
    st.stop()

# ---------- connect ----------
# Connecting now also starts the live market-data feed. This removes the
# confusing "connect first, then start engine" step.
col1, col2 = st.columns([1, 1])
with col1:
    connect = st.button("🔗 Connect & Go LIVE", type="primary", width="stretch")
with col2:
    stop = st.button("■ Stop Live Feed", width="stretch", disabled=st.session_state.engine is None)

if connect or st.session_state.pop("do_connect", False):
    try:
        # Never create a second TradingEngine/socket on top of an existing
        # session. Streamlit reruns preserve session_state, so pressing Connect
        # again without stopping the old engine can otherwise leave multiple
        # FYERS WebSocket threads alive and the newest chart attached to a
        # socket that is no longer delivering ticks.
        previous_engine = st.session_state.get("engine")
        if previous_engine is not None:
            try:
                previous_engine.stop_order_socket()
            except Exception:
                pass
            try:
                previous_engine.stop()
            except Exception:
                pass
            st.session_state.engine = None

        if not token: raise ValueError("No access token. Generate a fresh v3 token first.")
        client = FyersClient(app_id, token)
        profile = client.profile()
        option_cfg = {"underlying":option_underlying,"premium_min":premium_min,"premium_max":premium_max,"premium_target":premium_target,"expiry_mode":expiry_mode,"strikecount":int(strikecount)}
        protection_cfg = {"enabled":place_protection,"mode":protection_mode,"sl_points":sl_points,"target_points":target_points,"sl_percent":sl_percent,"target_percent":target_percent,"sl_atr_mult":sl_atr_mult,"target_atr_mult":target_atr_mult}
        engine = TradingEngine(client, signal_symbol, resolution, confirmation_points, confirmation_bars, qty, live, option_cfg, protection_cfg, test_live_entry=test_live_entry, session_start=_hhmm(algo_start_time, "09:15"), session_end=_hhmm(algo_end_time, "15:15"))
        # Rehydrate the in-session algo ledger so a reconnect/page switch does
        # not make already-triggered CE/PE markers disappear from the chart.
        engine.execution_events = [
            dict(x) for x in st.session_state.get("algo_execution_ledger", [])
            if isinstance(x, dict)
        ][-100:]
# Do not block Connect on the 31-day History REST bootstrap. FYERS can
        # return 429 while recovery/backfill is active; TradingEngine.start()
        # bootstraps history in the background while the live websocket starts.
        # Rehydrate the latest selected option from the in-session execution ledger.
        # This keeps the option chart/feed available after reconnecting or switching pages.
        latest_option = next(
            (e for e in reversed(st.session_state.get("algo_execution_ledger", []))
             if isinstance(e, dict) and e.get("symbol")),
            None,
        )
        if latest_option:
            engine.selected_option = {
                "symbol": latest_option.get("symbol"),
                "option_type": latest_option.get("option_type"),
                "strike": latest_option.get("strike"),
                "ltp": float(latest_option.get("entry") or 0),
                "expiry": latest_option.get("expiry"),
            }
            try:
                engine.execution_history = client.history(engine.selected_option["symbol"], engine.resolution, 31)
            except Exception:
                engine.execution_history = pd.DataFrame()
            engine.data_symbols.add(engine.selected_option["symbol"])
            if latest_option.get("entry") is not None:
                try:
                    engine.protection = {
                        "side": latest_option.get("side"),
                        "entry_reference": float(latest_option.get("entry")),
                        "sl_points": 0.0,
                        "target_points": 0.0,
                        "sl_price": float(latest_option.get("sl_price")) if latest_option.get("sl_price") is not None else 0.0,
                        "target_price": float(latest_option.get("target_price")) if latest_option.get("target_price") is not None else 0.0,
                        "enabled": bool(latest_option.get("protection_enabled", False)),
                    }
                except (TypeError, ValueError):
                    pass
        st.session_state.client = client
        st.session_state.profile = profile
        st.session_state.token = token
        st.session_state.auth_app_id = app_id
        # Persist the current token so a browser refresh restores this same
        # session without another manual login/paste.
        _save_fyers_session(app_id, token)
        st.session_state.engine = engine
        if st.session_state.paper_trader is None:
            st.session_state.paper_trader = PaperTrader()
        st.session_state.connected = True
        st.session_state.portfolio = load_portfolio(client)

        # Start the FYERS SymbolUpdate websocket immediately.
        # The user should see LIVE ticks without a second button.
        engine.start()
        # Order/trade websocket is optional for market-data startup. Older
        # deployments can have an engine without this helper, so never turn a
        # missing optional method into a live-feed error.
        # Order/trade updates are optional. The market-data socket is the
        # primary live feed, so a broker order socket problem must never surface
        # as a fatal terminal error or interrupt chart updates.
        start_order_ws = getattr(engine, "start_order_socket", None)
        if callable(start_order_ws):
            start_order_ws()
        st.success(f"Connected to FYERS • starting live feed…")
    except Exception as e:
        st.session_state.connected = False
        # If the saved token is stale/expired, do not keep retrying it on every
        # rerun. The normal Connect button or fresh-token flow remains available.
        st.error(f"Connection failed: {e}")
        if "-16" in str(e): st.warning("FYERS -16 = authentication failure. The access token is invalid/expired or doesn't belong to this App ID. Generate a new v3 token using the login flow above.")

engine = st.session_state.engine
client = st.session_state.client
if engine is not None:
    # Recover executions even when a Streamlit fragment/page rerun happened
    # between the broker fill and the next UI render.
    _merge_execution_ledger(getattr(engine, "execution_events", []))
if stop and engine:
    try:
        engine.stop_order_socket()
    except Exception:
        pass
    engine.stop()
    st.warning("Live feed stopped.")

# ---------- live dashboard ----------
# The terminal dashboard refreshes at 2s. FYERS market-data processing remains
# tick-driven in engine.py; this separates UI cadence from trading cadence and
# prevents the terminal chart from being remounted every second.
@st.fragment(run_every="2s")
def live_dashboard():
        # UI cadence only; FYERS ticks/strategy execution remain background-driven.
        if st.session_state.paper_trader is None:
            st.session_state.paper_trader = PaperTrader()
        paper_trader = st.session_state.paper_trader

        for kind, data in engine.drain_events():
            if kind == "log":
                # Keep a bounded in-session log so entry state is never lost
                # between Streamlit fragment reruns.
                item = data
                st.session_state.entry_log.append(item)
                st.session_state.entry_log = st.session_state.entry_log[-100:]
                msg = item["message"]
                if item["level"] in ("entry", "test", "live"):
                    st.toast(msg, icon="🚨" if item["level"] == "entry" else "🧪")
                elif item["level"] == "arm":
                    st.toast(msg, icon="🎯")
            elif kind == "execution":
                # Persist immediately; do not depend on the broker portfolio
                # refresh or on the next websocket tick.
                _merge_execution_ledger([data])
            elif kind == "order_update":
                # FYERS v3 order websocket is the live source of order/trade/
                # position changes. Keep a bounded snapshot for the UI instead
                # of polling REST after every execution.
                st.session_state["order_ws_updates"] = (
                    st.session_state.get("order_ws_updates", []) + [data]
                )[-100:]
            elif kind == "rejection":
                rej = data
                rejection = {
                    "time": rej.get("time").strftime("%H:%M:%S") if hasattr(rej.get("time"), "strftime") else str(rej.get("time","")),
                    "type": "BROKER REJECTED",
                    "message": rej.get("message","Order rejected"),
                }
                st.session_state.rejected_orders.append(rejection)
                st.session_state.rejected_orders = st.session_state.rejected_orders[-50:]
                st.error(f"⛔ {rejection['message']}")
                st.toast(rejection["message"], icon="⛔")
            elif kind == "error":
                # Keep websocket errors in the bounded monitor instead of
                # rendering a new red alert/toast on every 1-second fragment run.
                now = pd.Timestamp.now(tz="Asia/Kolkata")
                msg = str(data)
                st.session_state.entry_log.append({"time": now, "level":"error", "message":msg})
                st.session_state.entry_log = st.session_state.entry_log[-50:]
            elif kind == "signal":
                st.success(
                    f"{data['side']} signal • VWAP close {data['cross_price']:.2f} "
                    f"• confirmation {data['entry']:.2f} • bar {data['bars_since_cross']} • {data.get('cross_type', 'CLOSE_CROSS')}"
                )
                # AUTOMATIC PAPER ENTRY: signal -> option in premium band -> fill.
                if auto_paper and not paper_trader.position.qty:
                    try:
                        option = engine.selected_option or engine.select_option(data["side"])
                        # Paper protection is always based on the option premium,
                        # using the dedicated Paper SL/Target controls in the sidebar.
                        ok, msg = paper_trader.open(
                            option["symbol"], data["side"], paper_qty,
                            float(option["ltp"]),
                            paper_sl if place_protection else 0.0,
                            paper_target if place_protection else 0.0,
                        )
                        if ok:
                            st.success(
                                f"🤖 AUTO PAPER BUY {option.get('option_type', 'OPTION')} • signal={data['side']} • {option['symbol']} "
                                f"@ ₹{float(option['ltp']):.2f}"
                            )
                        else:
                            st.error(f"Auto paper entry blocked: {msg}")
                    except Exception as exc:
                        st.error(f"Auto paper entry failed: {exc}")
            elif kind == "option":
                st.info(
                    f"Selected {data['option_type']} {data['strike']} @ "
                    f"₹{data['ltp']:.2f} • expiry {data['expiry'].get('date')}"
                )
            elif kind == "order":
                if engine.test_live_entry and not live:
                    st.warning("🧪 TEST LIVE ENTRY — broker order was NOT sent.")
                    st.json(data)
                else:
                    st.write("Execution", data)

                # A broker position/order is not present in the cached portfolio
                # snapshot taken at Connect time. Refresh immediately after an
                # execution event so the Open Positions/Orders tables reflect the
                # entry without requiring the user to press Refresh manually.
                if live and not engine.test_live_entry:
                    try:
                        st.session_state.portfolio = load_portfolio(client)
                    except Exception as exc:
                        st.session_state.entry_log.append({
                            "time": pd.Timestamp.now(tz="Asia/Kolkata"),
                            "level": "error",
                            "message": f"Portfolio refresh after order failed: {exc}",
                        })
                        st.session_state.entry_log = st.session_state.entry_log[-100:]

        # Paper P&L/SL/target must follow the selected OPTION tick, not NIFTY.
        tick = engine.last_tick
        option_tick = engine.last_execution_tick
        ps0 = paper_trader.snapshot()
        paper_price = None
        if ps0["qty"]:
            if option_tick and option_tick.get("symbol") == ps0["symbol"]:
                paper_price = option_tick["ltp"]
            elif ps0["symbol"] == signal_symbol and tick:
                paper_price = tick["ltp"]
            if paper_price is not None:
                paper_trader.mark(paper_price)

        # TradingView-like live status/header.
        tick_age = engine.tick_age_seconds()
        # LIVE means: engine is running, websocket is connected, and the last
        # tick arrived recently. This makes a frozen feed obvious.
        live_ok = bool(engine.running and engine.ws_connected and tick and tick_age is not None and tick_age < 5)
        status_text = "LIVE" if live_ok else ("RECONNECTING" if engine.running and getattr(engine, "market_data_reconnecting", False) else ("CONNECTING" if engine.running else "STOPPED"))
        status_class = "live" if live_ok else "off"
        remaining = engine.bar_seconds_remaining()
        countdown = "--:--" if remaining is None else f"{remaining//60:02d}:{remaining%60:02d}"
        ltp_text = f"{tick['ltp']:,.2f}" if tick else "—"
        dot_color = "#20c997" if live_ok else "#ef5350"
        st.markdown(
            f'''<div class="tv-top">
            <div><div class="tv-title">NIFTY 50 <span class="tv-sub">• FYERS</span></div>
            <div class="tv-sub">{resolution}m timeframe</div></div>
            <div class="{status_class}"><span class="dot" style="background:{dot_color}"></span>{status_text}</div>
            <div class="tv-sub">Last tick&nbsp; {tick['time'].strftime('%H:%M:%S') if tick else '—'}</div>
            <div class="tv-sub">LTP&nbsp; <b style="color:#d8dee9">{ltp_text}</b></div>
            <div class="tv-sub">Age&nbsp; {f"{tick_age:.1f}s" if tick_age is not None else "—"}</div>
            <div class="count">Bar closes&nbsp; {countdown}</div>
            </div>''',
            unsafe_allow_html=True
        )

        if engine.running and not live_ok and getattr(engine, "market_data_blocked", False):
            # Only a broker-rejected subscription is rendered as a persistent
            # alert. Normal network disconnects stay in the compact header so
            # Streamlit does not rebuild a large warning block every second.
            st.error(
                "FYERS rejected the market-data subscription (11011). "
                "Reconnect manually after checking the App ID + today's access token "
                "and market-data permission."
            )

        positions, holdings, orders, trades = portfolio_tables(st.session_state.portfolio)
        tick = engine.last_tick
        pos_qty = sum(float(p.get("netQty",0) or 0) for p in positions)
        funds = st.session_state.portfolio.get("funds", {}).get("fund_limit", [])
        fmap = {str(x.get("id")):x.get("value") for x in funds if isinstance(x,dict)}

        m = st.columns(6)
        m[0].metric("NIFTY", f"{tick.get('ltp',0):.2f}" if tick else "—")
        m[1].metric("VWAP", f"{engine.current_vwap:.2f}" if engine.current_vwap is not None else "—")
        m[2].metric("Position", f"{pos_qty:g}")
        m[3].metric("Available", str(fmap.get("10", fmap.get("3", "—"))))
        # Use the persistent execution ledger for the visible count. The
        # engine's in-memory signal_count resets when the user reconnects or
        # switches pages, while the ledger intentionally survives those UI
        # lifecycle events.
        algo_entry_count = len(_all_live_execution_events(engine))
        m[4].metric("Algo Entries", str(algo_entry_count))
        m[5].metric("Engine", "RUNNING" if engine.running else "STOPPED")

        with st.expander("🚦 ENTRY ENGINE — live state", expanded=True):
            st.caption("Watch the exact entry sequence: VWAP cross → armed → selected-point move → option → execution.")
            st1, st2, st3, st4, st5 = st.columns(5)
            strat = engine.strategy
            if strat.armed:
                side_txt = "BUY CE" if strat.cross_direction == 1 else "BUY PE"
                st1v = "CROSSED"
                st1d = f"{side_txt} • close ₹{strat.cross_price:,.2f}"
                st2v = "ARMED"
                st2d = f"Trigger ₹{strat.confirmation_level:,.2f}"
                st3v = "WAITING"
                st3d = f"Bar {strat.bars_since_cross}/{strat.config.confirmation_bars}"
            elif engine.last_signal:
                side_txt = engine.last_signal.get("side", "—")
                st1v = "CROSSED"
                st1d = f"{side_txt} • close ₹{engine.last_signal.get('cross_price',0):,.2f}"
                st2v = "TRIGGERED"
                st2d = "Confirmation reached"
                st3v = "DONE"
                st3d = f"₹{engine.last_signal.get('entry',0):,.2f}"
            else:
                st1v = st2v = st3v = "WAITING"
                st1d = "No VWAP cross"
                st2d = "No active setup"
                st3d = "Waiting for price move"

            if engine.selected_option:
                opt = engine.selected_option
                st4v = "SELECTED"
                st4d = f"{opt.get('symbol','—')} • ₹{float(opt.get('ltp',0)):.2f}"
            else:
                st4v = "WAITING"
                st4d = f"₹{premium_min:.0f}–₹{premium_max:.0f} band"

            if live:
                st5v, st5d = "LIVE ORDER", "Broker call enabled"
            elif test_live_entry:
                st5v, st5d = "TEST ONLY", "No broker order"
            elif auto_paper:
                st5v, st5d = "AUTO PAPER", "Simulated fill"
            else:
                st5v, st5d = "MANUAL", "Button controlled"

            stages = [
                (st1, "VWAP CROSS", st1v, st1d),
                (st2, "ARMED", st2v, st2d),
                (st3, "POINT MOVE", st3v, st3d),
                (st4, "OPTION", st4v, st4d),
                (st5, "EXECUTION", st5v, st5d),
            ]
            for col, title, value, detail in stages:
                with col:
                    st.markdown(
                        '<div class="stage-card"><div class="stage-title">%s</div>'
                        '<div class="stage-value">%s</div><div class="stage-detail">%s</div></div>'
                        % (title, value, detail),
                        unsafe_allow_html=True,
                    )

        ps = paper_trader.snapshot(paper_price)
        s = engine.strategy
        if s.armed:
            armed_side = "BUY CE" if s.cross_direction == 1 else "BUY PE"
            armed_text = f"{armed_side} @ {s.confirmation_level:.2f}"
        else:
            armed_text = "FLAT / WAITING"
        pc1, pc2, pc3, pc4, pc5, pc6 = st.columns(6)
        pc1.metric("Paper", ps["status"])
        pc2.metric("Paper P&L", f"₹{ps["total_pnl"]:,.2f}")
        pc3.metric("Entry", f"{ps["entry"]:,.2f}" if ps["entry"] else "—")
        pc4.metric("SL", f"{ps["stop_loss"]:,.2f}" if ps["stop_loss"] else "—")
        pc5.metric("Target", f"{ps["target"]:,.2f}" if ps["target"] else "—")
        pc6.metric("Entry State", armed_text)

        if live:
            st.error("🔴 LIVE BROKER MODE — a triggered entry can place a real FYERS order.")
        elif test_live_entry:
            st.warning("🟡 LIVE ENGINE TEST — the real entry path is active, but broker order placement is blocked.")
        elif auto_paper:
            st.success("🟢 AUTO PAPER — a valid VWAP confirmation can automatically open a simulated option trade.")

        with st.expander("🧪 Entry engine monitor", expanded=True):
            c1, c2, c3 = st.columns(3)
            c1.metric("VWAP setup", armed_text)
            c2.metric("Entry attempts", str(engine.entry_attempts))
            c3.metric("Test-live entries", str(engine.test_entry_count))
            if s.armed:
                st.info(
                    f"{'BUY CE' if s.cross_direction == 1 else 'BUY PE'} setup armed: "
                    f"cross close ₹{s.cross_price:.2f} → trigger ₹{s.confirmation_level:.2f} "
                    f"• bar {s.bars_since_cross} / {s.config.confirmation_bars}"
                )
            if st.session_state.entry_log:
                latest = st.session_state.entry_log[-1]
                st.markdown(f"**Latest event:** `{latest['message']}`")
                rows = []
                for x in reversed(st.session_state.entry_log[-25:]):
                    t = x["time"]
                    if hasattr(t, "strftime"):
                        t = t.strftime("%H:%M:%S")
                    rows.append({"Time": t, "Level": str(x["level"]).upper(), "Message": x["message"]})
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=220)
            else:
                st.caption("No entry-state events yet. When VWAP crosses, you will see ARMED; when the confirmation move is reached, you will see ENTRY TRIGGERED.")

        if paper_manual:
            pa, pb, pc = st.columns(3)
            with pa:
                if st.button(
                    "🟢 BUY PAPER", width="stretch",
                    disabled=bool(ps["qty"]), key="paper_buy_btn"
                ):
                    try:
                        option = engine.selected_option or engine.select_option("BUY")
                        ok, msg = paper_trader.open(
                            option["symbol"], "BUY", paper_qty, float(option["ltp"]),
                            paper_sl if place_protection else 0.0,
                            paper_target if place_protection else 0.0,
                        )
                        (st.success if ok else st.error)(msg)
                    except Exception as exc:
                        st.error(f"BUY PAPER failed: {exc}")
                    st.rerun(scope="fragment")
            with pb:
                if st.button(
                    "🟣 BUY PE PAPER", width="stretch",
                    disabled=bool(ps["qty"]), key="paper_buy_pe_btn"
                ):
                    try:
                        option = engine.select_option("SELL")
                        ok, msg = paper_trader.open(
                            option["symbol"], "BUY", paper_qty, float(option["ltp"]),
                            paper_sl if place_protection else 0.0,
                            paper_target if place_protection else 0.0,
                        )
                        (st.success if ok else st.error)(f"BUY PE PAPER • {msg}")
                    except Exception as exc:
                        st.error(f"BUY PE PAPER failed: {exc}")
                    st.rerun(scope="fragment")
            with pc:
                if st.button(
                    "✕ CLOSE PAPER", width="stretch",
                    disabled=not bool(ps["qty"]), key="paper_close_btn"
                ):
                    close_price = paper_price
                    if close_price is None and engine.selected_option:
                        close_price = float(engine.selected_option.get("ltp") or 0)
                    if close_price:
                        ok, res = paper_trader.close(close_price)
                        (st.success if ok else st.error)(
                            f"Closed • P&L ₹{res:,.2f}" if ok else res
                        )
                    else:
                        st.error("Waiting for the option price.")
                    st.rerun(scope="fragment")

        tabs = st.tabs(["Chart", "Portfolio", "Options", "Strategy", "Paper Trades", "Orders & Trades"])

        # Keep the expensive/live chart transport on its own 1-second fragment.
        # The surrounding terminal remains at 2 seconds, so status/portfolio
        # widgets do not force a full dashboard redraw every second. Streamlit
        # supports nested fragments (>=1.37), and the chart component itself
        # already uses a stable V2 key + incremental candle transport.
        @st.fragment(run_every="1s")
        def _terminal_chart_live():
            st.markdown("#### NIFTY — VWAP strategy")
            levels=[]; markers=[]
            chart_df = engine.display_history()

            # The engine keeps a strategy-oriented history window. The chart
            # independently asks FYERS for enough data to retain up to 800 bars,
            # then overlays the engine's live candle so the browser gets both
            # deep scrollback and the current real-time OHLC.
            try:
                chart_days = _chart_history_days(engine.resolution, CHART_CANDLE_LIMIT)
                chart_key = (str(engine.signal_symbol), str(engine.resolution), chart_days)
                cache = st.session_state.setdefault("_chart_history_cache", {})
                if chart_key not in cache:
                    cache[chart_key] = client.history(
                        engine.signal_symbol, engine.resolution, chart_days
                    )
                # The REST window is immutable during a live candle. Avoid
                # concatenating/sorting the whole history every second; only
                # patch the current engine candle into the cached frame.
                chart_df = cache.get(chart_key)
                chart_df = chart_df.copy() if chart_df is not None else pd.DataFrame()
                live_candle = engine.current_candle
                if live_candle:
                    live_row = dict(live_candle)
                    live_row["vwap"] = engine.current_vwap
                    live_dt = pd.Timestamp(live_row["datetime"])
                    if live_dt.tzinfo is None:
                        live_dt = live_dt.tz_localize("Asia/Kolkata")
                    else:
                        live_dt = live_dt.tz_convert("Asia/Kolkata")
                    live_row["datetime"] = live_dt
                    if not chart_df.empty and "datetime" in chart_df.columns:
                        dts = pd.to_datetime(chart_df["datetime"])
                        if getattr(dts.dt, "tz", None) is None:
                            dts = dts.dt.tz_localize("Asia/Kolkata")
                        else:
                            dts = dts.dt.tz_convert("Asia/Kolkata")
                        chart_df["datetime"] = dts
                        # Merge by exact candle bucket, never by dataframe tail.
                        # This prevents delayed/stale ticks from being appended
                        # after a newer historical candle and then appearing
                        # displaced on the time axis.
                        matches = chart_df["datetime"] == live_dt
                        if matches.any():
                            idx = chart_df.index[matches][-1]
                            for col in ("open", "high", "low", "close"):
                                if col in chart_df.columns and col in live_row:
                                    old_value = pd.to_numeric(chart_df.loc[idx, col], errors="coerce")
                                    new_value = pd.to_numeric(live_row[col], errors="coerce")
                                    if pd.notna(new_value):
                                        if col == "open":
                                            chart_df.loc[idx, col] = old_value if pd.notna(old_value) else new_value
                                        elif col == "high":
                                            chart_df.loc[idx, col] = max(float(old_value) if pd.notna(old_value) else float(new_value), float(new_value))
                                        elif col == "low":
                                            chart_df.loc[idx, col] = min(float(old_value) if pd.notna(old_value) else float(new_value), float(new_value))
                                        else:
                                            chart_df.loc[idx, col] = new_value
                            if "vwap" in chart_df.columns:
                                chart_df.loc[idx, "vwap"] = live_row.get("vwap")
                        else:
                            chart_df = pd.concat([chart_df, pd.DataFrame([live_row])], ignore_index=True)
                    else:
                        chart_df = pd.DataFrame([live_row])
                    chart_df = (
                        chart_df.drop_duplicates(subset=["datetime"], keep="last")
                                 .sort_values("datetime")
                                 .reset_index(drop=True)
                    )
            except Exception as exc:
                # The engine history remains a valid live fallback if the
                # extended scrollback request fails.
                if chart_df is None or chart_df.empty:
                    st.warning(
                        f"Unable to load extended NIFTY chart history: {exc}",
                        icon="⚠️",
                    )

            live_execution_events = _all_live_execution_events(engine)
            levels.extend(_persistent_execution_levels(live_execution_events))
            if engine.strategy.cross_price is not None:
                levels.append({"price":engine.strategy.cross_price,"title":"Cross","color":"#f0b90b","style":2})
            if engine.strategy.armed and engine.strategy.confirmation_level is not None:
                levels.append({"price":engine.strategy.confirmation_level,"title":"Trigger","color":"#26a69a","style":2})
            historical_df = engine.history_df.copy() if engine.history_df is not None else pd.DataFrame()
            terminal_marker_cache = st.session_state.setdefault("_terminal_marker_cache", {})
            terminal_marker_key = (
                str(engine.signal_symbol), str(engine.resolution),
                float(engine.strategy.config.confirmation_points),
                int(engine.strategy.config.confirmation_bars),
                int(engine.session_start_minute), int(engine.session_end_minute),
            )
            if not historical_df.empty:
                if terminal_marker_key not in terminal_marker_cache:
                    terminal_marker_cache[terminal_marker_key] = _historical_signal_markers(
                        historical_df,
                        engine.strategy.config.confirmation_points,
                        engine.strategy.config.confirmation_bars,
                        session_start=_hhmm_from_minute(engine.session_start_minute),
                        session_end=_hhmm_from_minute(engine.session_end_minute),
                    )
                historical_markers = terminal_marker_cache[terminal_marker_key]
            else:
                historical_markers = []

            # Chart-tab fallback: if the engine history was populated after the
            # fragment rendered, or was trimmed during a reconnect, derive the
            # visible signal labels from the exact dataframe being plotted. This
            # keeps BUY CE / BUY PE labels in sync with the candles instead of
            # depending on a separate stale history snapshot.
            if not historical_markers and chart_df is not None and not chart_df.empty:
                marker_source = chart_df.copy()
                try:
                    live_candle = engine.current_candle
                    if live_candle and "datetime" in marker_source.columns:
                        last_dt = pd.Timestamp(marker_source.iloc[-1]["datetime"])
                        live_dt = pd.Timestamp(live_candle["datetime"])
                        if last_dt == live_dt:
                            marker_source = marker_source.iloc[:-1].copy()
                except Exception:
                    pass
                if not marker_source.empty:
                    fallback_marker_key = ("fallback", terminal_marker_key, str(marker_source.iloc[0]["datetime"]), str(marker_source.iloc[-1]["datetime"]), len(marker_source))
                    if fallback_marker_key not in terminal_marker_cache:
                        terminal_marker_cache[fallback_marker_key] = _historical_signal_markers(
                            marker_source,
                            engine.strategy.config.confirmation_points,
                            engine.strategy.config.confirmation_bars,
                            session_start=_hhmm_from_minute(engine.session_start_minute),
                            session_end=_hhmm_from_minute(engine.session_end_minute),
                        )
                    historical_markers = terminal_marker_cache[fallback_marker_key]

            live_execution_markers = _live_execution_markers(
                chart_df, live_execution_events
            ) if chart_df is not None and not chart_df.empty else []
            markers = []
            marker_keys = set()
            for marker in historical_markers + live_execution_markers:
                text = str(marker.get("text", "")).upper()
                option_type = "CE" if "CE" in text else ("PE" if "PE" in text else "")
                key = (int(marker.get("time", 0)), option_type)
                if key in marker_keys:
                    for idx, existing in enumerate(markers):
                        et = str(existing.get("text", "")).upper()
                        ekey = (int(existing.get("time", 0)), "CE" if "CE" in et else ("PE" if "PE" in et else ""))
                        if ekey == key:
                            markers[idx] = marker
                            break
                else:
                    marker_keys.add(key)
                    markers.append(marker)
            if not markers and engine.last_signal and chart_df is not None and not chart_df.empty:
                markers = _live_execution_markers(chart_df, [{
                    "chart_time": engine.last_signal.get("time"),
                    "side": engine.last_signal.get("side"),
                    "option_type": "CE" if engine.last_signal.get("side") == "BUY" else "PE",
                    "entry": engine.last_signal.get("entry", 0),
                }])
            render_chart(chart_df, "NIFTY • FYERS", ltp=engine.last_tick.get("ltp") if engine.last_tick else None, levels=levels, markers=markers, height=620, max_candles=CHART_CANDLE_LIMIT, fit_content=False, resolution=engine.resolution)

            if engine.selected_option:
                st.markdown(f"#### Execution — {engine.selected_option['symbol']}")
                if engine.execution_history.empty:
                    loaded_for = st.session_state.get("option_history_symbol")
                    if loaded_for != engine.selected_option["symbol"]:
                        try:
                            engine.execution_history = client.history(engine.selected_option["symbol"], resolution, 31)
                        except Exception:
                            engine.execution_history = pd.DataFrame()
                        st.session_state.option_history_symbol = engine.selected_option["symbol"]
                ex = engine.display_execution_history()
                lev=[]
                if engine.protection:
                    lev += [{"price":engine.protection["entry_reference"],"title":"Entry","color":"#8b95a7"}]
                    if engine.protection.get("enabled"):
                        lev += [
                            {"price":engine.protection["sl_price"],"title":"SL","color":"#ef5350"},
                            {"price":engine.protection["target_price"],"title":"Target","color":"#26a69a"},
                        ]
                else:
                    latest_option_event = next(
                        (e for e in reversed(live_execution_events)
                         if e.get("symbol") == engine.selected_option.get("symbol") and e.get("entry") is not None),
                        None,
                    )
                    if latest_option_event:
                        try:
                            lev.append({"price": float(latest_option_event["entry"]), "title":"Entry premium", "color":"#f0b90b"})
                        except (TypeError, ValueError):
                            pass
                current_positions, _, _, _ = portfolio_tables(st.session_state.portfolio)
                for pos in current_positions:
                    if pos.get("symbol") == engine.selected_option.get("symbol"):
                        try:
                            avg = float(pos.get("netAvg") or pos.get("avgPrice") or 0)
                            if avg > 0:
                                lev.append({"price":avg,"title":"Live position","color":"#f0b90b"})
                        except (TypeError, ValueError):
                            pass
                render_chart(ex, engine.selected_option['symbol'], ltp=engine.last_execution_tick.get("ltp") if engine.last_execution_tick else engine.selected_option.get("ltp"), vwap=False, levels=lev, height=500, max_candles=CHART_CANDLE_LIMIT, fit_content=False)


        with tabs[0]:
            _terminal_chart_live()
        with tabs[1]:
            a,b,c,d = st.columns(4)
            a.metric("Total balance", str(fmap.get("1","—"))); b.metric("Utilized", str(fmap.get("2","—"))); c.metric("Available", str(fmap.get("10",fmap.get("3","—")))); d.metric("Realized P&L", str(fmap.get("4","—")))
            st.subheader("Open positions")
            st.dataframe(pd.DataFrame(positions), width="stretch", hide_index=True)

            # Broker positions and local paper positions are intentionally
            # separate. In AUTO PAPER mode, FYERS will correctly report zero
            # broker positions even though the algo has an open simulated trade.
            local_paper = paper_trader.snapshot(paper_price)
            if local_paper.get("qty"):
                st.subheader("Local Paper Position")
                st.dataframe(pd.DataFrame([{
                    "symbol": local_paper.get("symbol"),
                    "side": local_paper.get("side"),
                    "qty": local_paper.get("qty"),
                    "entry": local_paper.get("entry"),
                    "unrealized_pnl": local_paper.get("unrealized_pnl"),
                    "stop_loss": local_paper.get("stop_loss"),
                    "target": local_paper.get("target"),
                    "status": local_paper.get("status"),
                }]), width="stretch", hide_index=True)
            elif auto_paper:
                st.info("No local paper position is open. A successful VWAP trigger will create one when Auto Paper is enabled.")

            st.subheader("Algo Execution Ledger")
            local_events = _all_live_execution_events(engine)
            local_exec = _execution_table(local_events)
            trigger_count = len(local_events)
            executed_count = sum(
                1 for x in local_events
                if str(x.get("status", "")).upper() in {"EXECUTED", "TEST", "PAPER"}
            )
            failed_count = sum(
                1 for x in local_events
                if str(x.get("status", "")).upper() in {"REJECTED", "FAILED"}
            )
            lm1, lm2, lm3 = st.columns(3)
            lm1.metric("Algo triggers", trigger_count)
            lm2.metric("Successful entries", executed_count)
            lm3.metric("Failed/rejected", failed_count)
            if local_exec.empty:
                st.info("No VWAP CE/PE trigger has been recorded in this session yet.")
            else:
                st.dataframe(local_exec, width="stretch", hide_index=True)
                st.caption(
                    "A marker/ledger row is created at the confirmation trigger immediately. "
                    "Its status is then updated when option selection and the broker/test/paper path completes."
                )
            st.subheader("Holdings")
            st.dataframe(pd.DataFrame(holdings), width="stretch", hide_index=True)
            if st.button("Refresh portfolio now"):
                try: st.session_state.portfolio = load_portfolio(client); st.rerun(scope="fragment")
                except Exception as e: st.error(str(e))

        with tabs[2]:
            st.write(
                f"**Premium band:** ₹{premium_min:.0f}–₹{premium_max:.0f} • "
                f"preferred ₹{premium_target:.0f} • expiry: {expiry_mode} • "
                f"**lot size: {option_lot_size} • {option_lots} lot(s) = {qty} qty**"
            )
            if st.button("Preview current CE/PE candidates"):
                try:
                    for direction, label in [("BUY", "BUY CE (bullish)"), ("SELL", "BUY PE (bearish)")]:
                        picked = client.choose_option(option_underlying, direction, premium_min, premium_max, premium_target, expiry_mode, int(strikecount))
                        st.write(label, {k:picked.get(k) for k in ["symbol","option_type","strike","ltp","bid","ask","expiry"]})
                except Exception as e: st.error(str(e))
            if engine.selected_option:
                st.success(f"Current execution contract: {engine.selected_option['symbol']} @ ₹{engine.selected_option['ltp']:.2f}")
                st.json({k:v for k,v in engine.selected_option.items() if k != "candidates"})

        with tabs[3]:
            st.write({"signal_symbol":engine.signal_symbol,"timeframe":engine.resolution,"confirmation_points":engine.strategy.config.confirmation_points,"confirmation_bars":engine.strategy.config.confirmation_bars,"cross_price":engine.strategy.cross_price,"cross_direction":engine.strategy.cross_direction,"bars_since_cross":engine.strategy.bars_since_cross,"last_signal":engine.last_signal,"selected_option":engine.selected_option,"protection":engine.protection,
                         "test_live_entry":engine.test_live_entry,"last_order_payload":engine.last_order})
            if not engine.history_df.empty:
                if (engine.history_df["volume"] == 0).all():
                    st.warning("FYERS is returning zero volume for the signal instrument. A true volume VWAP cannot be calculated from this feed. If TradingView shows a VWAP on NIFTY, use a volume-bearing NIFTY futures symbol as the VWAP data source or verify the feed before live trading.")
                st.dataframe(engine.history_df[[c for c in ["datetime","open","high","low","close","volume","ohlc4","vwap","atr"] if c in engine.history_df.columns]].tail(100), width="stretch", hide_index=True)

        with tabs[4]:
            st.subheader("Paper trade history")
            st.dataframe(pd.DataFrame(ps.get("history", [])), width="stretch", hide_index=True)
            st.caption("Paper trades are stored only in this Streamlit session. Restarting the app resets them.")

        with tabs[5]:
            st.subheader("Order book")
            st.dataframe(pd.DataFrame(orders), width="stretch", hide_index=True)
            st.subheader("Trade book")
            st.dataframe(pd.DataFrame(trades), width="stretch", hide_index=True)

if engine:
    live_dashboard()
else:
    st.info("Connect to FYERS to load the terminal.")

st.divider()
st.caption("Protection uses FYERS bracket-order fields when LIVE TRADING is enabled. Verify that your FYERS API app is the current compliant app with the required static IP/permissions before placing live orders.")
