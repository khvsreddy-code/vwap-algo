import os
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

from fyers_client import FyersClient
from engine import TradingEngine

load_dotenv()
st.set_page_config(page_title="FYERS VWAP Trading Terminal", layout="wide")

st.title("FYERS VWAP Trading Terminal")
st.caption("FYERS market data + your PineScript VWAP confirmation engine")

# ---------- session state ----------
for key, default in {
    "client": None,
    "engine": None,
    "profile": None,
    "token": os.getenv("FYERS_ACCESS_TOKEN", ""),
    "history": pd.DataFrame(),
    "portfolio": {},
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ---------- sidebar ----------
with st.sidebar:
    st.header("FYERS Authentication")
    app_id = st.text_input("App ID / Client ID", value=os.getenv("FYERS_APP_ID", ""))
    token = st.text_input("Access token", value=st.session_state.token, type="password")

    with st.expander("Generate a fresh v3 token", expanded=False):
        secret_id = st.text_input("Secret ID", value=os.getenv("FYERS_SECRET_ID", ""), type="password")
        redirect_uri = st.text_input("Redirect URI", value=os.getenv("FYERS_REDIRECT_URI", ""))
        state = st.text_input("State", value="fyers_vwap")
        if st.button("Generate Login URL", use_container_width=True):
            try:
                url = FyersClient.auth_url(app_id, secret_id, redirect_uri, state)
                st.session_state.auth_url = url
                st.success("Open the URL, authorize FYERS, then paste the auth_code below.")
            except Exception as exc:
                st.error(str(exc))
        if st.session_state.get("auth_url"):
            st.code(st.session_state.auth_url, language="text")
        auth_code = st.text_input("auth_code from redirect", type="password")
        if st.button("Exchange auth_code", use_container_width=True):
            try:
                new_token = FyersClient.exchange_auth_code(app_id, secret_id, redirect_uri, auth_code)
                st.session_state.token = new_token
                token = new_token
                st.success("Access token generated. Click Connect / Refresh.")
                st.code(new_token, language="text")
            except Exception as exc:
                st.error(f"Token generation failed: {exc}")

    st.divider()
    st.header("Strategy")
    signal_symbol = st.text_input("Signal symbol", value=os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX"))
    execution_symbol = st.text_input("Execution symbol", value=os.getenv("FYERS_EXECUTION_SYMBOL", ""))
    resolution = st.selectbox("Timeframe", ["1", "3", "5", "10", "15", "30", "60"], index=2)
    confirmation_points = st.number_input("Confirmation points", min_value=1.0, value=15.0, step=0.5)
    confirmation_bars = st.number_input("Maximum confirmation bars", min_value=1, max_value=20, value=5)
    qty = st.number_input("Quantity", min_value=1, value=1, step=1)

    st.header("Execution")
    live = st.checkbox("LIVE TRADING", value=False)
    if live:
        st.error("LIVE ORDERS ENABLED")
    else:
        st.info("Dry-run: no order will be sent.")

# ---------- connection ----------
c1, c2, c3 = st.columns(3)
with c1:
    connect = st.button("Connect / Refresh", use_container_width=True)
with c2:
    start = st.button("Start Live Engine", use_container_width=True, disabled=st.session_state.engine is None)
with c3:
    stop = st.button("Stop Engine", use_container_width=True)

if connect:
    try:
        if not token:
            raise ValueError("No access token. Generate a fresh v3 token or paste today's token.")
        client = FyersClient(app_id, token)
        profile = client.profile()  # authentication test
        st.session_state.client = client
        st.session_state.profile = profile
        st.session_state.token = token
        engine = TradingEngine(
            client=client,
            signal_symbol=signal_symbol,
            execution_symbol=execution_symbol or signal_symbol,
            resolution=resolution,
            confirmation_points=confirmation_points,
            confirmation_bars=confirmation_bars,
            qty=qty,
            live_trading=live,
        )
        df = engine.load_history()
        st.session_state.engine = engine
        st.session_state.history = df
        st.session_state.portfolio = load_portfolio(client)
        st.success(f"Connected as {profile.get('data', {}).get('name', 'FYERS user')}.")
    except Exception as exc:
        st.error(f"Connection failed: {exc}")
        if "-16" in str(exc):
            st.warning("FYERS -16 means the token is invalid/expired. Generate a fresh v3 access token and make sure the App ID belongs to the same API app that created that token.")

engine = st.session_state.engine
client = st.session_state.client

if start and engine:
    try:
        engine.start()
        st.success("Live market-data engine started.")
    except Exception as exc:
        st.error(f"Start failed: {exc}")

if stop and engine:
    engine.stop()
    st.warning("Stop requested.")

# ---------- helpers ----------
def load_portfolio(c):
    result = {}
    for name, fn in [("funds", c.funds), ("positions", c.positions), ("holdings", c.holdings), ("orders", c.orders), ("trades", c.trades)]:
        try:
            result[name] = fn()
        except Exception as exc:
            result[name] = {"s": "error", "message": str(exc)}
    return result


def extract_list(response, keys):
    if not isinstance(response, dict):
        return []
    data = response.get("data", response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []


def portfolio_tables(portfolio):
    positions = extract_list(portfolio.get("positions", {}), ["netPositions", "positions"])
    holdings = extract_list(portfolio.get("holdings", {}), ["holdings"])
    orders = extract_list(portfolio.get("orders", {}), ["orderBook", "orders"])
    trades = extract_list(portfolio.get("trades", {}), ["tradeBook", "trades"])
    return positions, holdings, orders, trades


def build_chart(df, positions, last_tick):
    if df is None or df.empty:
        return None
    plot = df.tail(180).copy()
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=plot["datetime"], open=plot["open"], high=plot["high"],
        low=plot["low"], close=plot["close"], name="FYERS OHLC"
    ))
    if "vwap" in plot:
        fig.add_trace(go.Scatter(x=plot["datetime"], y=plot["vwap"], mode="lines", name="VWAP"))

    # Current/known positions are displayed on the chart when their symbol matches the signal symbol.
    for p in positions:
        psym = p.get("symbol") or p.get("fyToken")
        if psym == (engine.signal_symbol if engine else None):
            avg = p.get("netAvg") or p.get("avgPrice") or p.get("buyAvg") or p.get("sellAvg")
            qty = p.get("netQty") or p.get("qty")
            try:
                avg = float(avg)
                qty = float(qty)
                fig.add_hline(y=avg, line_dash="dash", annotation_text=f"Position {qty:g} @ {avg:g}")
            except (TypeError, ValueError):
                pass

    if last_tick:
        fig.add_hline(y=last_tick["ltp"], line_dash="dot", annotation_text=f"LTP {last_tick['ltp']:.2f}")

    fig.update_layout(height=600, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=35, b=10))
    return fig


if engine:
    for kind, data in engine.drain_events():
        if kind == "error":
            st.error(data)
        elif kind == "signal":
            st.success(f"SIGNAL: {data['side']} @ {data['entry']:.2f} | cross={data['cross_price']:.2f} | bars={data['bars_since_cross']}")
        elif kind == "order":
            st.write("Order result", data)

    # Top status cards
    tick = engine.last_tick
    positions, holdings, orders, trades = portfolio_tables(st.session_state.portfolio)
    status_cols = st.columns(5)
    with status_cols[0]:
        st.metric("LTP", f"{tick['ltp']:.2f}" if tick else "—")
    with status_cols[1]:
        st.metric("VWAP", f"{engine.current_vwap:.2f}" if engine.current_vwap is not None else "—")
    with status_cols[2]:
        st.metric("Position", str(sum(float(p.get("netQty", 0) or 0) for p in positions)))
    with status_cols[3]:
        st.metric("Signals", str(engine.signal_count))
    with status_cols[4]:
        st.metric("Engine", "RUNNING" if engine.running else "STOPPED")

    tabs = st.tabs(["Chart", "Portfolio", "Strategy", "Orders & Trades", "Raw Data"])

    with tabs[0]:
        fig = build_chart(st.session_state.get("history"), positions, tick)
        if fig:
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No FYERS candles available yet.")
        st.caption("Chart is rendered from FYERS OHLC/history data; VWAP and live LTP are overlaid. Position lines appear when FYERS reports a matching position.")

    with tabs[1]:
        pcols = st.columns(4)
        funds_data = st.session_state.portfolio.get("funds", {}).get("fund_limit", [])
        funds_map = {str(x.get("id")): x.get("value") for x in funds_data if isinstance(x, dict)}
        pcols[0].metric("Total balance", str(funds_map.get("1", "—")))
        pcols[1].metric("Utilized", str(funds_map.get("2", "—")))
        pcols[2].metric("Available", str(funds_map.get("3", "—")))
        pcols[3].metric("Realized P&L", str(funds_map.get("4", "—")))

        st.subheader("Open positions")
        st.dataframe(pd.DataFrame(positions), use_container_width=True, hide_index=True)
        st.subheader("Holdings")
        st.dataframe(pd.DataFrame(holdings), use_container_width=True, hide_index=True)

    with tabs[2]:
        st.write({
            "signal_symbol": engine.signal_symbol,
            "execution_symbol": engine.execution_symbol,
            "timeframe": engine.resolution,
            "confirmation_points": engine.strategy.config.confirmation_points,
            "confirmation_bars": engine.strategy.config.confirmation_bars,
            "cross_price": engine.strategy.cross_price,
            "cross_direction": engine.strategy.cross_direction,
            "bars_since_cross": engine.strategy.bars_since_cross,
            "trade_active": engine.strategy.trade_active,
            "last_signal": engine.last_signal,
            "last_order": engine.last_order,
        })

    with tabs[3]:
        st.subheader("Order book")
        st.dataframe(pd.DataFrame(orders), use_container_width=True, hide_index=True)
        st.subheader("Trade book")
        st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)

    with tabs[4]:
        df = st.session_state.get("history")
        if df is not None and not df.empty:
            cols = [c for c in ["datetime", "open", "high", "low", "close", "volume", "ohlc4", "vwap"] if c in df.columns]
            st.dataframe(df[cols].tail(200), use_container_width=True, hide_index=True)
            if "volume" in df and (df["volume"] == 0).all():
                st.warning("FYERS returned zero volume for this signal symbol. A true volume VWAP cannot be reproduced from that feed.")

    if st.button("Refresh Portfolio", use_container_width=True):
        try:
            st.session_state.portfolio = load_portfolio(client)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
else:
    st.info("Connect first. If you see FYERS error -16, generate a fresh v3 token in the Authentication expander.")

st.divider()
st.caption("Dry-run is the default. Verify signal parity and portfolio/order reconciliation before enabling live trading.")
