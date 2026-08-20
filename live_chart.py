
"""Persistent TradingView-style chart for the Streamlit terminal.

Uses Streamlit Custom Components V2 instead of components.html().
The chart DOM/Lightweight Charts instance is kept alive while Python fragments
rerun, so live candles are updated in-place rather than replacing the iframe.
"""
import json
import pandas as pd
import streamlit as st

_CHART_HTML = """
<div id="vwap-live-chart" style="width:100%;height:100%;"></div>
"""

_CHART_CSS = """
:host, #vwap-live-chart {
  display:block;
  width:100%;
  height:100%;
  min-height:520px;
  background:#0b0f14;
  overflow:hidden;
}
"""

_CHART_JS = r"""
export default function(component) {
  const root = component.parentElement;
  const data = component.data || {};
  let host = root.querySelector("#vwap-live-chart");
  if (!host) {
    host = document.createElement("div");
    host.id = "vwap-live-chart";
    host.style.width = "100%";
    host.style.height = "100%";
    root.appendChild(host);
  }

  const state = host.__vwapState || {
    chart: null,
    candles: null,
    vwap: null,
    ltpLine: null,
    levelLines: [],
    ready: false,
    loading: false,
    lastPayload: null,
    resizeObserver: null,
  };
  host.__vwapState = state;

  function loadScript() {
    if (window.LightweightCharts) return Promise.resolve();
    if (state.loading && state.loadPromise) return state.loadPromise;
    state.loading = true;
    state.loadPromise = new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-vwap-lwc="1"]');
      if (existing) {
        existing.addEventListener("load", () => resolve());
        existing.addEventListener("error", reject);
        return;
      }
      const script = document.createElement("script");
      script.dataset.vwapLwc = "1";
      script.src = "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js";
      script.onload = () => resolve();
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return state.loadPromise;
  }

  function normalisePayload(p) {
    return {
      candles: Array.isArray(p.candles) ? p.candles : [],
      vwap: Array.isArray(p.vwap) ? p.vwap : [],
      ltp: p.ltp == null ? null : Number(p.ltp),
      levels: Array.isArray(p.levels) ? p.levels : [],
      markers: Array.isArray(p.markers) ? p.markers : [],
      title: p.title || "",
      height: Number(p.height || 900),
      fitContent: Boolean(p.fitContent),
    };
  }

  function sameFirstAndLength(a, b) {
    return a.length === b.length &&
      a.length > 0 && b.length > 0 &&
      Number(a[0].time) === Number(b[0].time);
  }

  function syncSeries(series, incoming, stateKey) {
    const previous = state[stateKey + "Data"] || [];
    if (!previous.length || !sameFirstAndLength(previous, incoming)) {
      series.setData(incoming);
    } else if (incoming.length) {
      // Lightweight Charts' update() replaces the last bar when timestamps
      // match and appends when the timestamp advances.
      series.update(incoming[incoming.length - 1]);
    }
    state[stateKey + "Data"] = incoming;
  }

  function applyPayload(p) {
    const d = normalisePayload(p);
    if (!state.chart) return;

    syncSeries(state.candles, d.candles, "candle");
    if (state.vwap) {
      if (d.vwap.length) syncSeries(state.vwap, d.vwap, "vwap");
      else {
        state.vwap.setData([]);
        state.vwapData = [];
      }
    }

    if (state.ltpLine) {
      try { state.candles.removePriceLine(state.ltpLine); } catch(e) {}
      state.ltpLine = null;
    }
    if (d.ltp != null && Number.isFinite(d.ltp)) {
      state.ltpLine = state.candles.createPriceLine({
        price: d.ltp,
        color: "#8b95a7",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: "LTP",
      });
    }

    for (const line of state.levelLines) {
      try { state.candles.removePriceLine(line); } catch(e) {}
    }
    state.levelLines = [];
    for (const x of d.levels) {
      const price = Number(x.price);
      if (!Number.isFinite(price)) continue;
      state.levelLines.push(state.candles.createPriceLine({
        price,
        color: x.color || "#aab2bf",
        lineWidth: 1,
        lineStyle: x.style || 2,
        axisLabelVisible: true,
        title: x.title || "",
      }));
    }

    if (typeof state.candles.setMarkers === "function") {
      state.candles.setMarkers(d.markers || []);
    }

    if (!state.hasFit || d.fitContent) {
      state.chart.timeScale().fitContent();
      state.hasFit = true;
    }
    state.lastPayload = d;
  }

  function init() {
    if (state.ready) {
      applyPayload(data);
      return;
    }
    state.chart = LightweightCharts.createChart(host, {
      width: Math.max(320, host.clientWidth || 900),
      height: Math.max(200, host.clientHeight || data.height || 620),
      layout: {
        background: { type: "solid", color: "#0b0f14" },
        textColor: "#9aa4b2",
      },
      grid: {
        vertLines: { color: "#17202b" },
        horzLines: { color: "#17202b" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#263241", autoScale: true },
      timeScale: {
        borderColor: "#263241",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
        barSpacing: 8,
        lockVisibleTimeRangeOnResize: true,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true,
      },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: true,
      },
    });

    state.candles = state.chart.addCandlestickSeries({
      upColor: "#22b39b",
      downColor: "#ef5350",
      borderVisible: false,
      wickUpColor: "#22b39b",
      wickDownColor: "#ef5350",
      priceLineVisible: false,
      lastValueVisible: true,
    });
    state.vwap = state.chart.addLineSeries({
      color: "#4aa3ff",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });

    state.resizeObserver = new ResizeObserver(() => {
      if (!state.chart) return;
      const w = host.clientWidth;
      const h = host.clientHeight;
      if (w > 10 && h > 10) state.chart.applyOptions({ width: w, height: h });
    });
    state.resizeObserver.observe(host);
    state.ready = true;
    applyPayload(data);
  }

  loadScript().then(init).catch(() => {
    host.textContent = "Unable to load the chart library.";
  });

  // V2 component renderers may be invoked repeatedly; do not destroy the chart.
  // Cleanup happens only when the component is genuinely unmounted.
  return () => {
    // Deliberately do not remove the chart during normal Python reruns.
    // Streamlit calls cleanup only when the component instance is unmounted.
  };
}
"""

try:
    _component = st.components.v2.component(
        name="fyers_vwap_live_chart",
        html=_CHART_HTML,
        css=_CHART_CSS,
        js=_CHART_JS,
        isolate_styles=True,
    )
except AttributeError:
    _component = None


def make_payload(
    df, vwap=True, ltp=None, levels=None, markers=None, title="",
    height=900, max_candles=90, fit_content=False
):
    if df is None or df.empty:
        return {
            "candles": [], "vwap": [], "ltp": ltp,
            "levels": levels or [], "markers": markers or [],
            "title": title, "height": height, "fitContent": bool(fit_content),
        }

    # Live charts keep a small rolling window for performance. Replay and
    # historical/backtest views can pass max_candles=None to render the entire
    # dataframe, so moving the replay slider to candle 1245 actually displays
    # candles 1..1245 instead of silently truncating to the last 90.
    plot = df if max_candles is None else df.tail(int(max_candles))
    candles = []
    vw = []
    for r in plot.itertuples():
        ts = int(pd.Timestamp(r.datetime).timestamp())
        candles.append({
            "time": ts,
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
        })
        if vwap and hasattr(r, "vwap") and pd.notna(getattr(r, "vwap")):
            vw.append({"time": ts, "value": float(getattr(r, "vwap"))})

    return {
        "candles": candles,
        "vwap": vw,
        "ltp": None if ltp is None else float(ltp),
        "levels": levels or [],
        "markers": markers or [],
        "title": title,
        "height": int(height),
        "fitContent": bool(fit_content),
    }


def render(
    df, title, height=900, vwap=True, ltp=None, levels=None, markers=None,
    max_candles=90, fit_content=False
):
    payload = make_payload(
        df, vwap=vwap, ltp=ltp, levels=levels, markers=markers,
        title=title, height=height, max_candles=max_candles,
        fit_content=fit_content
    )
    if _component is None:
        st.error("This live chart requires Streamlit Custom Components V2. Upgrade Streamlit to 1.52+.")
        return
    # Streamlit Components V2 support explicit layout dimensions.  Without
    # this, the wrapper defaults to content/auto height and the chart is
    # rendered at a much smaller height than the requested value.
    _component(
        data=payload,
        key=f"live-{title}-{height}",
        width="stretch",
        height=int(height),
    )
