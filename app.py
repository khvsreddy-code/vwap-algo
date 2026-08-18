import os
import streamlit as st
from dotenv import load_dotenv

from fyers_client import FyersClient
from engine import TradingEngine

load_dotenv()
st.set_page_config(page_title="FYERS VWAP Entry Engine", layout="wide")

st.title("FYERS VWAP Entry Engine")
st.caption("Python execution engine based on your PineScript VWAP confirmation rules")

with st.sidebar:
    st.header("FYERS")
    app_id = st.text_input("App ID", value=os.getenv("FYERS_APP_ID", ""), type="password")
    token = st.text_input("Access token", value=os.getenv("FYERS_ACCESS_TOKEN", ""), type="password")
    st.caption("Use your current FYERS API v3 compliant app/token. Do not commit secrets to source control.")

    st.header("Strategy")
    symbol = st.text_input("Signal symbol", value=os.getenv("FYERS_SIGNAL_SYMBOL", "NSE:NIFTY50-INDEX"))
    execution_symbol = st.text_input("Execution symbol", value=os.getenv("FYERS_EXECUTION_SYMBOL", ""))
    resolution = st.selectbox("Timeframe", ["1", "3", "5", "10", "15", "30", "60"], index=2)
    confirmation_points = st.number_input("Confirmation points", min_value=1.0, value=15.0, step=0.5)
    confirmation_bars = st.number_input("Maximum confirmation bars", min_value=1, max_value=20, value=5)
    qty = st.number_input("Quantity", min_value=1, value=1, step=1)

    st.header("Execution")
    live = st.checkbox("LIVE TRADING", value=False)
    if live:
        st.error("LIVE ORDERS ENABLED — verify symbol, quantity, product and risk controls before starting.")
    else:
        st.info("Paper/dry-run mode. No order is sent to FYERS.")

if "engine" not in st.session_state:
    st.session_state.engine = None

c1, c2, c3 = st.columns(3)
with c1:
    connect = st.button("Connect / Load Data", use_container_width=True)
with c2:
    start = st.button("Start Live Engine", use_container_width=True, disabled=st.session_state.engine is None)
with c3:
    stop = st.button("Stop Engine", use_container_width=True)

if connect:
    try:
        client = FyersClient(app_id, token)
        profile = client.profile()
        st.session_state.profile = profile
        st.session_state.engine = TradingEngine(
            client=client,
            signal_symbol=symbol,
            execution_symbol=execution_symbol or symbol,
            resolution=resolution,
            confirmation_points=confirmation_points,
            confirmation_bars=confirmation_bars,
            qty=qty,
            live_trading=live,
        )
        df = st.session_state.engine.load_history()
        st.session_state.history = df
        st.success("FYERS connected and history loaded.")
    except Exception as exc:
        st.error(f"Connection failed: {exc}")

engine = st.session_state.engine

if start and engine:
    try:
        engine.start()
        st.success("Live market-data engine started.")
    except Exception as exc:
        st.error(f"Start failed: {exc}")

if stop and engine:
    engine.stop()
    st.warning("Stop requested. Restart Streamlit if the SDK socket does not release cleanly.")

if engine:
    events = engine.drain_events()
    for kind, data in events:
        if kind == "error":
            st.error(data)
        elif kind == "signal":
            st.success(f"SIGNAL: {data['side']} at {data['entry']:.2f} | cross={data['cross_price']:.2f} | bars={data['bars_since_cross']}")
        elif kind == "order":
            st.write("Order result", data)

    left, right = st.columns(2)
    with left:
        st.subheader("Market")
        tick = engine.last_tick
        if tick:
            st.metric("LTP", f"{tick['ltp']:.2f}")
            st.caption(f"{tick['symbol']} · {tick['time'].strftime('%H:%M:%S')}")
        else:
            st.info("Waiting for live tick...")

    with right:
        st.subheader("Strategy state")
        st.write({
            "running": engine.running,
            "cross_price": engine.strategy.cross_price,
            "cross_direction": engine.strategy.cross_direction,
            "trade_active": engine.strategy.trade_active,
            "last_signal": engine.last_signal,
            "last_order": engine.last_order,
        })

    st.subheader("Historical candles / VWAP")
    df = st.session_state.get("history")
    if df is not None and not df.empty:
        cols = [c for c in ["datetime", "open", "high", "low", "close", "volume", "vwap"] if c in df.columns]
        st.dataframe(df[cols].tail(100), use_container_width=True, hide_index=True)
        if (df["volume"] == 0).all():
            st.warning("FYERS returned zero volume for the signal symbol. Classic volume VWAP cannot be reproduced from this feed. Consider using a traded futures/ETF instrument for a true volume VWAP.")

        if st.button("Replay history and show signals"):
            signals = engine.process_history_signal_test()
            if signals:
                st.dataframe(signals, use_container_width=True, hide_index=True)
            else:
                st.info("No confirmation signals found in the loaded history.")
else:
    st.info("Enter your FYERS App ID and access token, then click Connect / Load Data.")

st.divider()
st.caption("This is an execution-engine prototype. Validate signal parity against TradingView and use dry-run mode before enabling live orders.")
