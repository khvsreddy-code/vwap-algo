import os
import json
import html
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from fyers_client import FyersClient
from engine import TradingEngine

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

load_dotenv()
st.set_page_config(page_title="FYERS VWAP Trader", page_icon="📈", layout="wide", initial_sidebar_state="expanded")

# ---------- session state ----------
def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

for key, default in {
    "client": None, "engine": None, "profile": None, "token": os.getenv("FYERS_ACCESS_TOKEN", ""),
    "portfolio": {}, "auth_url": "", "last_error": "", "connected": False,
}.items(): ss(key, default)

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
</style>
""", unsafe_allow_html=True)

st.title("FYERS VWAP Trader")
st.caption("TradingView-style market terminal • Pine VWAP confirmation • FYERS execution")

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
        st.info("Use this only when your current token has expired or you get FYERS error -16.")
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
            st.caption("After authorization, FYERS redirects you to your Redirect URI.")

        auth_code = st.text_input(
            "5. Paste the auth_code from the redirected URL",
            type="password",
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


def chart_html(df, title, vwap=True, ltp=None, levels=None, markers=None, height=600):
    if df is None or df.empty: return ""
    plot = df.tail(300).copy()
    candles = []
    for _, r in plot.iterrows():
        candles.append({"time": int(pd.Timestamp(r["datetime"]).timestamp()), "open":float(r["open"]), "high":float(r["high"]), "low":float(r["low"]), "close":float(r["close"])})
    vw = []
    if vwap and "vwap" in plot:
        for _, r in plot.iterrows():
            if pd.notna(r.get("vwap")): vw.append({"time":int(pd.Timestamp(r["datetime"]).timestamp()), "value":float(r["vwap"])})
    payload = {"title":title, "candles":candles, "vwap":vw, "ltp":ltp, "levels":levels or [], "markers":markers or []}
    raw = json.dumps(payload, default=str).replace("</", "<\\/")
    page = f'''<!doctype html><html><head><meta charset="utf-8"><script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<style>html,body,#c{{margin:0;width:100%;height:100%;background:#0b0f14;overflow:hidden}}#c{{height:{height}px}}#title{{position:absolute;z-index:5;left:12px;top:8px;color:#d8dee9;font:600 13px Inter,Arial}}</style></head><body><div id="title"></div><div id="c"></div>
<script>
const d={raw}; document.getElementById('title').textContent=d.title;
const el=document.getElementById('c');
const chart=LightweightCharts.createChart(el,{{width:el.clientWidth,height:{height},layout:{{background:{{type:'solid',color:'#0b0f14'}},textColor:'#9aa4b2'}},grid:{{vertLines:{{color:'#1b222c'}},horzLines:{{color:'#1b222c'}}}},crosshair:{{mode:LightweightCharts.CrosshairMode.Normal}},rightPriceScale:{{borderColor:'#2a313b',autoScale:true}},timeScale:{{borderColor:'#2a313b',timeVisible:true,secondsVisible:false,rightOffset:8,barSpacing:8}},handleScroll:{{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:true}},handleScale:{{mouseWheel:true,pinch:true,axisPressedMouseMove:true}}}});
const cs=chart.addCandlestickSeries({{upColor:'#26a69a',downColor:'#ef5350',borderVisible:false,wickUpColor:'#26a69a',wickDownColor:'#ef5350'}}); cs.setData(d.candles);
if(d.vwap.length){{const vs=chart.addLineSeries({{color:'#5da9ff',lineWidth:2,lastValueVisible:true,priceLineVisible:false}});vs.setData(d.vwap);}}
if(d.ltp){{cs.createPriceLine({{price:Number(d.ltp),color:'#8b95a7',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:'LTP'}});}}
for(const x of d.levels){{cs.createPriceLine({{price:Number(x.price),color:x.color||'#aab2bf',lineWidth:1,lineStyle:x.style||2,axisLabelVisible:true,title:x.title||''}});}}
if(d.markers.length){{cs.setMarkers(d.markers);}}
chart.timeScale().fitContent();
new ResizeObserver(()=>chart.applyOptions({{width:el.clientWidth}})).observe(el);
</script></body></html>'''
    return page


def render_chart(df, title, **kwargs):
    components.html(chart_html(df, title, **kwargs), height=kwargs.get("height", 600)+4, scrolling=False)

# ---------- connect ----------
col1, col2, col3 = st.columns([1,1,1])
with col1: connect = st.button("Connect / Refresh", type="primary", use_container_width=True)
with col2: start = st.button("▶ Start Engine", use_container_width=True, disabled=st.session_state.engine is None)
with col3: stop = st.button("■ Stop Engine", use_container_width=True, disabled=st.session_state.engine is None)

if connect or st.session_state.pop("do_connect", False):
    try:
        if not token: raise ValueError("No access token. Generate a fresh v3 token first.")
        client = FyersClient(app_id, token)
        profile = client.profile()
        option_cfg = {"underlying":option_underlying,"premium_min":premium_min,"premium_max":premium_max,"premium_target":premium_target,"expiry_mode":expiry_mode,"strikecount":int(strikecount)}
        protection_cfg = {"enabled":place_protection,"mode":protection_mode,"sl_points":sl_points,"target_points":target_points,"sl_percent":sl_percent,"target_percent":target_percent,"sl_atr_mult":sl_atr_mult,"target_atr_mult":target_atr_mult}
        engine = TradingEngine(client, signal_symbol, resolution, confirmation_points, confirmation_bars, qty, live, option_cfg, protection_cfg)
        engine.load_history()
        st.session_state.client = client; st.session_state.profile = profile; st.session_state.token = token; st.session_state.engine = engine; st.session_state.connected = True
        st.session_state.portfolio = load_portfolio(client)
        st.success(f"Connected: {profile.get('data',{}).get('name','FYERS user')}")
    except Exception as e:
        st.session_state.connected = False
        st.error(f"Connection failed: {e}")
        if "-16" in str(e): st.warning("FYERS -16 = authentication failure. The access token is invalid/expired or doesn't belong to this App ID. Generate a new v3 token using the login flow above.")

engine = st.session_state.engine
client = st.session_state.client
if start and engine:
    try: engine.start(); st.success("Engine started")
    except Exception as e: st.error(f"Start failed: {e}")
if stop and engine:
    engine.stop(); st.warning("Engine stopped")

if engine and engine.running and st_autorefresh:
    st_autorefresh(interval=2000, key="terminal_refresh")

# ---------- dashboard ----------
if engine:
    for kind, data in engine.drain_events():
        if kind == "error": st.error(data)
        elif kind == "signal": st.success(f"{data['side']} signal • cross {data['cross_price']:.2f} • confirmation {data['entry']:.2f} • bar {data['bars_since_cross']}")
        elif kind == "option": st.info(f"Selected {data['option_type']} {data['strike']} @ ₹{data['ltp']:.2f} • expiry {data['expiry'].get('date')}")
        elif kind == "order": st.write("Order", data)

    # Refresh portfolio periodically while live; otherwise use last snapshot.
    if st_autorefresh and engine.running:
        try: st.session_state.portfolio = load_portfolio(client)
        except Exception: pass

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

    tabs = st.tabs(["Chart", "Portfolio", "Options", "Strategy", "Orders & Trades"])
    with tabs[0]:
        st.markdown("#### NIFTY — VWAP strategy")
        levels=[]; markers=[]
        if engine.strategy.cross_price is not None:
            levels.append({"price":engine.strategy.cross_price,"title":"Cross","color":"#f0b90b","style":2})
        if engine.last_signal:
            markers.append({"time":int(pd.Timestamp(engine.last_signal['time']).timestamp()),"position":"belowBar" if engine.last_signal['side']=="BUY" else "aboveBar","color":"#26a69a" if engine.last_signal['side']=="BUY" else "#ef5350","shape":"arrowUp" if engine.last_signal['side']=="BUY" else "arrowDown","text":engine.last_signal['side']})
        render_chart(engine.history_df, "NIFTY • FYERS", ltp=tick.get("ltp") if tick else None, levels=levels, markers=markers, height=620)

        if engine.selected_option:
            st.markdown(f"#### Execution — {engine.selected_option['symbol']}")
            ex = engine.execution_history if not engine.execution_history.empty else client.history(engine.selected_option['symbol'], resolution, 3)
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
            try: st.session_state.portfolio = load_portfolio(client); st.rerun()
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
        st.write({"signal_symbol":engine.signal_symbol,"timeframe":engine.resolution,"confirmation_points":engine.strategy.config.confirmation_points,"confirmation_bars":engine.strategy.config.confirmation_bars,"cross_price":engine.strategy.cross_price,"cross_direction":engine.strategy.cross_direction,"bars_since_cross":engine.strategy.bars_since_cross,"last_signal":engine.last_signal,"selected_option":engine.selected_option,"protection":engine.protection})
        if not engine.history_df.empty:
            if (engine.history_df["volume"] == 0).all():
                st.warning("FYERS is returning zero volume for the signal instrument. A true volume VWAP cannot be calculated from this feed. If TradingView shows a VWAP on NIFTY, use a volume-bearing NIFTY futures symbol as the VWAP data source or verify the feed before live trading.")
            st.dataframe(engine.history_df[[c for c in ["datetime","open","high","low","close","volume","ohlc4","vwap","atr"] if c in engine.history_df.columns]].tail(100), use_container_width=True, hide_index=True)

    with tabs[4]:
        st.subheader("Order book")
        st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
        st.subheader("Trade book")
        st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
else:
    st.info("Connect to FYERS to load the terminal.")

st.divider()
st.caption("Protection uses FYERS bracket-order fields when LIVE TRADING is enabled. Verify that your FYERS API app is the current compliant app with the required static IP/permissions before placing live orders.")
