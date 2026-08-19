import os
import json
import html
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from fyers_client import FyersClient
from engine import TradingEngine
from paper_trading import PaperTrader
from live_chart import render as render_chart


load_dotenv()
st.set_page_config(page_title="FYERS VWAP Trader", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

# ---------- session state ----------
def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

for key, default in {
    "client": None, "engine": None, "profile": None, "token": os.getenv("FYERS_ACCESS_TOKEN", ""),
    "portfolio": {}, "auth_url": "", "last_error": "", "connected": False,
    "paper_trader": None, "entry_log": [], "callback_auth_code": "", "show_auth_callback": False, "rejected_orders": [], "option_history_symbol": None,
}.items(): ss(key, default)

# ---------- simple FYERS auth callback ----------
_callback_code = st.query_params.get("auth_code")
if _callback_code:
    st.session_state.callback_auth_code = str(_callback_code)
    st.session_state.show_auth_callback = True
    st.query_params.clear()

if st.session_state.show_auth_callback and st.session_state.callback_auth_code:
    st.markdown("## ✅ FYERS authorization complete")
    st.info("Your FYERS login worked. Copy the temporary auth code below and paste it into the terminal to get today's access token.")
    st.markdown("### Your auth code")
    st.code(st.session_state.callback_auth_code, language=None)
    st.caption("This code is temporary. Do not share it with anyone.")
    if st.button("📋 I copied the auth code — back to terminal", type="primary"):
        st.session_state.show_auth_callback = False
        st.rerun()
    st.stop()

# ---------- styling ----------
st.markdown("""
<style>
.stApp { background: #0b0f14; color: #d8dee9; }
[data-testid="stHeader"] { background: rgba(11,15,20,0.92); }
.block-container { padding-top: 1rem; padding-bottom: 1rem; max-width: 1600px; }
section[data-testid="stSidebar"] { background: #11161d; border-right: 1px solid #222a35; }
.card { background:#11161d; border:1px solid #222a35; border-radius:10px; padding:12px 14px; }
.small { color:#8f9bad; font-size:12px; }
.good { color:#26a69a; font-weight:700; }
.bad { color:#ef5350; font-weight:700; }
.warn { color:#f0b90b; font-weight:700; }
.tv-top { display:flex; align-items:center; gap:18px; padding:10px 14px; border:1px solid #202833; border-radius:8px; background:#0d1218; margin-bottom:8px; }
.tv-title { font-size:16px; font-weight:700; color:#e7edf5; }
.tv-sub { font-size:12px; color:#8995a5; }
.dot { width:9px; height:9px; border-radius:50%; display:inline-block; margin-right:6px; }
.live { color:#20c997; font-weight:700; }
.off { color:#ef5350; font-weight:700; }
.count { margin-left:auto; font-variant-numeric:tabular-nums; font-weight:700; color:#e7edf5; }
.page-nav { display:flex; gap:6px; margin:0 0 12px 0; padding:4px; background:#10161d; border:1px solid #222a35; border-radius:10px; width:max-content; }
.page-nav a { color:#8995a5; text-decoration:none; padding:7px 14px; border-radius:7px; font-size:13px; font-weight:600; }
.page-nav a.active { background:#1b2633; color:#e7edf5; }
.chart-status { display:flex; align-items:center; gap:12px; padding:10px 14px; margin-bottom:8px; border:1px solid #222a35; border-radius:8px; background:#0d1218; color:#9aa7b7; font-size:12px; }
.chart-dot { width:9px; height:9px; border-radius:50%; display:inline-block; }
.chart-dot.on { background:#20c997; box-shadow:0 0 8px rgba(32,201,151,.45); }
.chart-dot.off { background:#ef5350; }
.chart-count { margin-left:auto; color:#e7edf5; font-weight:700; }
.mode-banner { margin:0 0 10px 0; padding:11px 14px; border:1px solid #2a3440; border-radius:10px; background:#10161d; display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.mode-banner.danger { border-color:#71373a; }
.mode-banner.test { border-color:#6c5b2b; }
.mode-banner.paper { border-color:#24564e; }
.mode-desc { color:#8f9bad; font-size:12px; }
.mode-chain { margin-left:auto; display:flex; gap:8px; align-items:center; color:#b9c3cf; font-size:11px; }
.mode-chain b { color:#697687; }
.stage-card { background:#0d1218; border:1px solid #222a35; border-radius:9px; padding:10px 12px; min-height:72px; }
.stage-title { font-size:11px; color:#8793a3; text-transform:uppercase; letter-spacing:.5px; }
.stage-value { font-size:15px; font-weight:700; margin-top:5px; color:#e7edf5; }
.stage-detail { font-size:11px; color:#7f8b9b; margin-top:3px; }
.reject-card { background:#1b1012; border:1px solid #6f2b31; border-radius:10px; padding:12px 14px; margin:8px 0; }
.reject-title { color:#ff6b6b; font-weight:800; font-size:13px; }
.reject-detail { color:#d8b4b7; font-size:12px; margin-top:4px; }
</style>
""", unsafe_allow_html=True)

# ---------- simple page navigation ----------
page = st.query_params.get("page", "terminal")
if page not in ("terminal", "charts"):
    page = "terminal"
st.markdown(
    '<div class="page-nav">'
    '<a class="%s" href="?page=terminal">⌂ Terminal</a>'
    '<a class="%s" href="?page=charts">📊 Charts</a>'
    '</div>' % ("active" if page == "terminal" else "", "active" if page == "charts" else ""),
    unsafe_allow_html=True,
)

# ---------- sidebar ----------
with st.sidebar:
    st.markdown("## 🔌 FYERS")
    st.caption("Connect once, then run the terminal.")

    # Simple connection: only two fields are needed when the user already has a token.
    app_id = st.text_input(
        "App ID",
        value=os.getenv("FYERS_APP_ID", ""),
        help="Your FYERS API App ID / Client ID.",
    )
    token = st.text_input(
        "Access token",
        value=st.session_state.token,
        type="password",
        help="Paste today's FYERS v3 access token here.",
    )

    if st.button("🔗 Connect to FYERS", type="primary", use_container_width=True):
        st.session_state.do_connect = True

    if st.session_state.get("connected"):
        st.success("Connected")
    else:
        st.caption("Don't have today's token?")

    # Advanced auth is deliberately hidden. Most users only need App ID + access token.
    with st.expander("Get a fresh access token", expanded=False):
        st.info("Use this when your token has expired or you see FYERS -16. After login, this app shows a simple auth-code page.")
        secret_id = st.text_input(
            "1. Secret ID",
            value=os.getenv("FYERS_SECRET_ID", ""),
            type="password",
        )
        redirect_uri = st.text_input(
            "2. Redirect URI",
            value=os.getenv("FYERS_REDIRECT_URI", ""),
            help="Must exactly match the Redirect URI registered in your FYERS API app.",
        )
        state = "fyers_vwap"

        if st.button("3. Create login link", use_container_width=True):
            try:
                st.session_state.auth_url = FyersClient.auth_url(app_id, secret_id, redirect_uri, state)
            except Exception as e:
                st.error(str(e))

        if st.session_state.auth_url:
            st.markdown("**4. Open this link, log in, and authorize the app:**")
            st.link_button("Open FYERS Login", st.session_state.auth_url, use_container_width=True)
            st.caption("After authorization, FYERS returns to this app and shows your auth code on a simple copy-friendly page.")

        auth_code = st.text_input(
            "5. Auth code",
            value=st.session_state.callback_auth_code,
            type="password",
            help="After FYERS redirects back, the app captures the code automatically.",
        )
        if st.button("6. Get today's token", use_container_width=True):
            try:
                if not secret_id or not redirect_uri or not auth_code:
                    raise ValueError("Enter Secret ID, Redirect URI and auth_code first.")
                new_token = FyersClient.exchange_auth_code(app_id, secret_id, redirect_uri, auth_code)
                st.session_state.token = new_token
                st.success("Token created. Click 'Connect to FYERS' above.")
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
    confirmation_points = st.number_input("Move after VWAP cross", 1.0, 100.0, 15.0, 0.5)
    confirmation_bars = st.number_input("Confirmation window (candles)", 1, 20, 5, 1)

    st.markdown("## 🎯 Option entry")
    option_underlying = st.text_input("Option chain", value=signal_symbol)
    premium_min = st.number_input("Premium min", 1.0, 1000.0, 180.0, 1.0)
    premium_max = st.number_input("Premium max", 1.0, 2000.0, 200.0, 1.0)
    premium_target = st.number_input("Preferred premium", premium_min, premium_max, 190.0, 1.0)
    expiry_mode = st.selectbox("Expiry", ["Nearest", "Monthly"], index=0)
    strikecount = st.number_input("Strike search", 1, 50, 25, 1)
    qty = st.number_input("Quantity", 1, 100000, 1, 1)

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
    paper_qty = st.number_input("Paper quantity", 1, 100000, int(qty), 1)
    paper_sl = st.number_input("Paper SL points", 0.0, 5000.0, float(sl_points or 20.0), 0.5)
    paper_target = st.number_input("Paper target points", 0.0, 10000.0, float(target_points or 40.0), 0.5)
# ---------- execution mode banner ----------
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
render_rejection_panel()

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
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=240)

def render_charts_page():
    engine = st.session_state.engine
    client = st.session_state.client
    if not engine:
        st.markdown("## 📊 Live Charts")
        st.info("Connect to FYERS from the Terminal page first.")
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

        has_option = bool(engine.selected_option)
        if "show_nifty_chart" not in st.session_state:
            st.session_state.show_nifty_chart = True
        if "show_option_chart" not in st.session_state:
            st.session_state.show_option_chart = True

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
                "text": engine.last_signal["side"],
            })

        def option_chart():
            symbol = engine.selected_option["symbol"]
            ex = engine.execution_history
            if ex is None or ex.empty:
                loaded_for = st.session_state.get("option_history_symbol")
                if loaded_for != symbol:
                    try:
                        engine.execution_history = client.history(symbol, engine.resolution, 3)
                    except Exception:
                        engine.execution_history = pd.DataFrame()
                    st.session_state.option_history_symbol = symbol
                ex = engine.execution_history
            lev = []
            if engine.protection:
                lev.append({"price": engine.protection["entry_reference"], "title": "Entry", "color": "#8b95a7"})
                if engine.protection.get("enabled"):
                    lev += [
                        {"price": engine.protection["sl_price"], "title": "SL", "color": "#ef5350"},
                        {"price": engine.protection["target_price"], "title": "Target", "color": "#26a69a"},
                    ]
            render_chart(ex, engine.selected_option["symbol"],
                         ltp=engine.last_execution_tick.get("ltp") if engine.last_execution_tick else engine.selected_option.get("ltp"),
                         vwap=False, levels=lev, height=620)

        if show_nifty and show_option and has_option:
            left, right = st.columns(2, gap="small")
            with left:
                st.markdown("#### NIFTY • VWAP")
                render_chart(engine.display_history(), "NIFTY • FYERS", ltp=ltp, levels=levels, markers=markers, height=620)
            with right:
                st.markdown(f"#### OPTION • {engine.selected_option['symbol']}")
                option_chart()
        elif show_nifty:
            st.markdown("#### NIFTY • VWAP")
            render_chart(engine.display_history(), "NIFTY • FYERS", ltp=ltp, levels=levels, markers=markers, height=720)
        elif show_option and has_option:
            st.markdown(f"#### OPTION • {engine.selected_option['symbol']}")
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
            st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
            st.subheader("Recent Orders")
            st.dataframe(pd.DataFrame(orders[-20:] if orders else []), use_container_width=True, hide_index=True)
            if st.button("↻ Refresh portfolio", key="charts_refresh_portfolio"):
                try:
                    st.session_state.portfolio = load_portfolio(client)
                    st.rerun(scope="fragment")
                except Exception as e:
                    st.error(str(e))

    _charts_live()

if page == "charts":
    render_charts_page()
    st.stop()

# ---------- connect ----------
# Connecting now also starts the live market-data feed. This removes the
# confusing "connect first, then start engine" step.
col1, col2 = st.columns([1, 1])
with col1:
    connect = st.button("🔗 Connect & Go LIVE", type="primary", use_container_width=True)
with col2:
    stop = st.button("■ Stop Live Feed", use_container_width=True, disabled=st.session_state.engine is None)

if connect or st.session_state.pop("do_connect", False):
    try:
        if not token: raise ValueError("No access token. Generate a fresh v3 token first.")
        client = FyersClient(app_id, token)
        profile = client.profile()
        option_cfg = {"underlying":option_underlying,"premium_min":premium_min,"premium_max":premium_max,"premium_target":premium_target,"expiry_mode":expiry_mode,"strikecount":int(strikecount)}
        protection_cfg = {"enabled":place_protection,"mode":protection_mode,"sl_points":sl_points,"target_points":target_points,"sl_percent":sl_percent,"target_percent":target_percent,"sl_atr_mult":sl_atr_mult,"target_atr_mult":target_atr_mult}
        engine = TradingEngine(client, signal_symbol, resolution, confirmation_points, confirmation_bars, qty, live, option_cfg, protection_cfg, test_live_entry=test_live_entry)
        engine.load_history()
        st.session_state.client = client
        st.session_state.profile = profile
        st.session_state.token = token
        st.session_state.engine = engine
        if st.session_state.paper_trader is None:
            st.session_state.paper_trader = PaperTrader()
        st.session_state.connected = True
        st.session_state.portfolio = load_portfolio(client)

        # Start the FYERS SymbolUpdate websocket immediately.
        # The user should see LIVE ticks without a second button.
        engine.start()
        st.success(f"Connected to FYERS • starting live feed…")
    except Exception as e:
        st.session_state.connected = False
        st.error(f"Connection failed: {e}")
        if "-16" in str(e): st.warning("FYERS -16 = authentication failure. The access token is invalid/expired or doesn't belong to this App ID. Generate a new v3 token using the login flow above.")

engine = st.session_state.engine
client = st.session_state.client
if stop and engine:
    engine.stop()
    st.warning("Live feed stopped.")

# ---------- live dashboard ----------
@st.fragment(run_every="1s")
def live_dashboard():
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
                    f"• confirmation {data['entry']:.2f} • bar {data['bars_since_cross']}"
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
                                f"🤖 AUTO PAPER {data['side']} • {option['symbol']} "
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
        m[4].metric("Signals", str(engine.signal_count))
        m[5].metric("Engine", "RUNNING" if engine.running else "STOPPED")

        with st.expander("🚦 ENTRY ENGINE — live state", expanded=True):
            st.caption("Watch the exact entry sequence: VWAP cross → armed → selected-point move → option → execution.")
            st1, st2, st3, st4, st5 = st.columns(5)
            strat = engine.strategy
            if strat.armed:
                side_txt = "BUY" if strat.cross_direction == 1 else "SELL"
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
            armed_side = "BUY" if s.cross_direction == 1 else "SELL"
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
                    f"{'BUY' if s.cross_direction == 1 else 'SELL'} setup armed: "
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
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=220)
            else:
                st.caption("No entry-state events yet. When VWAP crosses, you will see ARMED; when the confirmation move is reached, you will see ENTRY TRIGGERED.")

        if paper_manual:
            pa, pb, pc = st.columns(3)
            with pa:
                if st.button(
                    "🟢 BUY PAPER", use_container_width=True,
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
                    "🔴 SELL PAPER", use_container_width=True,
                    disabled=bool(ps["qty"]), key="paper_sell_btn"
                ):
                    try:
                        option = engine.selected_option or engine.select_option("SELL")
                        ok, msg = paper_trader.open(
                            option["symbol"], "SELL", paper_qty, float(option["ltp"]),
                            paper_sl if place_protection else 0.0,
                            paper_target if place_protection else 0.0,
                        )
                        (st.success if ok else st.error)(msg)
                    except Exception as exc:
                        st.error(f"SELL PAPER failed: {exc}")
                    st.rerun(scope="fragment")
            with pc:
                if st.button(
                    "✕ CLOSE PAPER", use_container_width=True,
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
        with tabs[0]:
            st.markdown("#### NIFTY — VWAP strategy")
            levels=[]; markers=[]
            if engine.strategy.cross_price is not None:
                levels.append({"price":engine.strategy.cross_price,"title":"Cross","color":"#f0b90b","style":2})
            if engine.last_signal:
                markers.append({"time":int(pd.Timestamp(engine.last_signal['time']).timestamp()),"position":"belowBar" if engine.last_signal['side']=="BUY" else "aboveBar","color":"#26a69a" if engine.last_signal['side']=="BUY" else "#ef5350","shape":"arrowUp" if engine.last_signal['side']=="BUY" else "arrowDown","text":engine.last_signal['side']})
            render_chart(engine.display_history(), "NIFTY • FYERS", ltp=tick.get("ltp") if tick else None, levels=levels, markers=markers, height=620)

            if engine.selected_option:
                st.markdown(f"#### Execution — {engine.selected_option['symbol']}")
                if engine.execution_history.empty:
                    loaded_for = st.session_state.get("option_history_symbol")
                    if loaded_for != engine.selected_option["symbol"]:
                        try:
                            engine.execution_history = client.history(engine.selected_option["symbol"], resolution, 3)
                        except Exception:
                            engine.execution_history = pd.DataFrame()
                        st.session_state.option_history_symbol = engine.selected_option["symbol"]
                ex = engine.execution_history
                lev=[]
                if engine.protection:
                    lev += [{"price":engine.protection["entry_reference"],"title":"Entry","color":"#8b95a7"}]
                    if engine.protection.get("enabled"):
                        lev += [
                            {"price":engine.protection["sl_price"],"title":"SL","color":"#ef5350"},
                            {"price":engine.protection["target_price"],"title":"Target","color":"#26a69a"},
                        ]
                for pos in positions:
                    if pos.get("symbol") == engine.selected_option.get("symbol"):
                        try:
                            avg = float(pos.get("netAvg") or pos.get("avgPrice") or 0)
                            if avg > 0:
                                lev.append({"price":avg,"title":"Live position","color":"#f0b90b"})
                        except (TypeError, ValueError):
                            pass
                render_chart(ex, engine.selected_option['symbol'], ltp=engine.last_execution_tick.get("ltp") if engine.last_execution_tick else engine.selected_option.get("ltp"), vwap=False, levels=lev, height=500)

        with tabs[1]:
            a,b,c,d = st.columns(4)
            a.metric("Total balance", str(fmap.get("1","—"))); b.metric("Utilized", str(fmap.get("2","—"))); c.metric("Available", str(fmap.get("10",fmap.get("3","—")))); d.metric("Realized P&L", str(fmap.get("4","—")))
            st.subheader("Open positions")
            st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
            st.subheader("Holdings")
            st.dataframe(pd.DataFrame(holdings), use_container_width=True, hide_index=True)
            if st.button("Refresh portfolio now"):
                try: st.session_state.portfolio = load_portfolio(client); st.rerun(scope="fragment")
                except Exception as e: st.error(str(e))

        with tabs[2]:
            st.write(f"**Premium band:** ₹{premium_min:.0f}–₹{premium_max:.0f} • preferred ₹{premium_target:.0f} • expiry: {expiry_mode}")
            if st.button("Preview current CE/PE candidates"):
                try:
                    for side in ["BUY","SELL"]:
                        picked = client.choose_option(option_underlying, side, premium_min, premium_max, premium_target, expiry_mode, int(strikecount))
                        st.write(side, {k:picked.get(k) for k in ["symbol","option_type","strike","ltp","bid","ask","expiry"]})
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
                st.dataframe(engine.history_df[[c for c in ["datetime","open","high","low","close","volume","ohlc4","vwap","atr"] if c in engine.history_df.columns]].tail(100), use_container_width=True, hide_index=True)

        with tabs[4]:
            st.subheader("Paper trade history")
            st.dataframe(pd.DataFrame(ps.get("history", [])), use_container_width=True, hide_index=True)
            st.caption("Paper trades are stored only in this Streamlit session. Restarting the app resets them.")

        with tabs[5]:
            st.subheader("Order book")
            st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
            st.subheader("Trade book")
            st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)

if engine:
    live_dashboard()
else:
    st.info("Connect to FYERS to load the terminal.")

st.divider()
st.caption("Protection uses FYERS bracket-order fields when LIVE TRADING is enabled. Verify that your FYERS API app is the current compliant app with the required static IP/permissions before placing live orders.")
