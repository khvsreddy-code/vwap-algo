
"""Persistent TradingView-style chart for the Streamlit terminal.

Uses Streamlit Custom Components V2 instead of components.html().
The chart DOM/Lightweight Charts instance is kept alive while Python fragments
rerun, so live candles are updated in-place rather than replacing the iframe.
"""
import json
import math
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


def _json_float(value):
    """Convert numeric values to finite Python floats for component transport."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _normalise_marker(marker):
    """Return a JSON-safe Lightweight Charts marker or None."""
    if not isinstance(marker, dict):
        return None
    try:
        ts = int(pd.Timestamp(marker.get("time")).timestamp())
    except (TypeError, ValueError, OverflowError):
        try:
            ts = int(marker.get("time"))
        except (TypeError, ValueError, OverflowError):
            return None

    position = str(marker.get("position") or "")
    if position not in {"aboveBar", "belowBar", "inBar"}:
        position = "inBar"

    shape = str(marker.get("shape") or "")
    if shape not in {"arrowUp", "arrowDown", "circle", "square"}:
        shape = "circle"

    out = {
        "time": ts,
        "position": position,
        "shape": shape,
        "text": str(marker.get("text") or ""),
    }
    color = marker.get("color")
    if color:
        out["color"] = str(color)
    if marker.get("size") is not None:
        try:
            out["size"] = max(1, int(marker["size"]))
        except (TypeError, ValueError):
            pass
    return out


def make_payload(
    df, vwap=True, ltp=None, levels=None, markers=None, title="",
    height=900, max_candles=90, fit_content=False
):
    if df is None or df.empty:
        return {
            "candles": [], "vwap": [], "ltp": _json_float(ltp),
            "levels": levels or [], "markers": [],
            "title": str(title), "height": int(height), "fitContent": bool(fit_content),
        }

    # Live charts keep a small rolling window for performance. Replay,
    # full-backtest, and historical views pass max_candles=None so the
    # complete currently-loaded window is sent to the browser.
    plot = df if max_candles is None else df.tail(int(max_candles))

    candles = []
    vw = []
    for r in plot.itertuples(index=False):
        try:
            ts = int(pd.Timestamp(getattr(r, "datetime")).timestamp())
        except (TypeError, ValueError, OverflowError):
            continue

        o = _json_float(getattr(r, "open", None))
        h = _json_float(getattr(r, "high", None))
        lo = _json_float(getattr(r, "low", None))
        c = _json_float(getattr(r, "close", None))
        # Lightweight Charts rejects malformed OHLC rows. Skip only rows
        # that cannot be represented safely; valid candles are preserved.
        if None in (o, h, lo, c):
            continue

        candles.append({
            "time": ts,
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
        })

        if vwap and hasattr(r, "vwap"):
            vv = _json_float(getattr(r, "vwap"))
            if vv is not None:
                vw.append({"time": ts, "value": vv})

    safe_levels = []
    for level in (levels or []):
        if not isinstance(level, dict):
            continue
        price = _json_float(level.get("price"))
        if price is None:
            continue
        safe_levels.append({
            "price": price,
            "title": str(level.get("title") or ""),
            "color": str(level.get("color") or "#aab2bf"),
            "style": int(level.get("style", 2)) if str(level.get("style", "")).isdigit() else 2,
        })

    safe_markers = []
    for marker in (markers or []):
        item = _normalise_marker(marker)
        if item is not None:
            safe_markers.append(item)

    return {
        "candles": candles,
        "vwap": vw,
        "ltp": _json_float(ltp),
        "levels": safe_levels,
        "markers": safe_markers,
        "title": str(title),
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

    # Keep the component instance stable across replay ticks. The data changes,
    # but the key does not, so Lightweight Charts can update the existing chart
    # rather than mounting a new component for every candle.
    component_key = f"live-{title}-{int(height)}"

    # Streamlit 1.52+ supports width/height on V2 component mounts. A fallback
    # without layout arguments keeps the chart working on older 1.52-era
    # runtimes that expose V2 but reject one of the newer layout keywords.
    try:
        _component(
            data=payload,
            key=component_key,
            width="stretch",
            height=int(height),
        )
    except TypeError as exc:
        # Do not let a layout-signature mismatch take down replay/backtest.
        # The component CSS supplies its own minimum height in this fallback.
        if "width" in str(exc) or "height" in str(exc) or "unexpected keyword" in str(exc):
            _component(data=payload, key=component_key)
        else:
            raise
