
"""Persistent TradingView-style chart for the Streamlit terminal.

v9.4.43 — live chart transport/stability fix.

Uses Streamlit Custom Components V2 instead of components.html().
The chart DOM/Lightweight Charts instance is kept alive while Python fragments
rerun, so live candles are updated in-place rather than replacing the iframe.
"""
import json
import math
import re
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
    // Monotonic sequence protects against an older component update arriving
    // after a newer one.
    payloadSeq: 0,
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

  function decodePayload(input) {
    // Keep the transport deliberately boring: Streamlit Custom Components V2
    // already serializes ordinary JSON.  The previous gzip/base64 layer added
    // an asynchronous decode race which could produce BidiComponent errors and
    // make the chart disappear/reappear during fragment reruns.
    if (!input || typeof input !== "object") return {};
    return input;
  }

  function normalisePayload(p) {
    p = p || {};
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

  function rowsEqual(a, b) {
    if (!a || !b) return false;
    if (Number(a.time) !== Number(b.time)) return false;
    const keys = ["open", "high", "low", "close", "value"];
    for (const k of keys) {
      if ((a[k] == null) !== (b[k] == null)) return false;
      if (a[k] != null && Number(a[k]) !== Number(b[k])) return false;
    }
    return true;
  }

  function arraysEqual(a, b) {
    if (a === b) return true;
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    // Compare the full timestamp/price envelope only when necessary. This is
    // deliberately cheap for the normal 31-day history because unchanged
    // payloads return immediately from the first/last checks below.
    if (!a.length) return true;
    if (!rowsEqual(a[0], b[0]) || !rowsEqual(a[a.length - 1], b[b.length - 1])) return false;
    if (a.length <= 2) return true;
    // A changed historical window is rare; verify all rows in that case.
    for (let i = 1; i < a.length - 1; i++) {
      if (!rowsEqual(a[i], b[i])) return false;
    }
    return true;
  }

  function syncSeries(series, incoming, stateKey) {
    const next = incoming || [];
    const previous = state[stateKey + "Data"] || [];

    if (arraysEqual(previous, next)) return;

    // Normal live tick: same history, only the current candle changed.
    if (previous.length && next.length === previous.length &&
        previous.length === next.length &&
        Number(previous[0].time) === Number(next[0].time)) {
      const last = next[next.length - 1];
      if (last && !rowsEqual(previous[previous.length - 1], last)) {
        try {
          series.update(last);
          state[stateKey + "Data"] = next.slice();
          return;
        } catch (e) {}
      }
    }

    // New candle rolled in or the historical window was reloaded.
    series.setData(next);
    state[stateKey + "Data"] = next.slice();
  }

  function sameJson(a, b) {
    try { return JSON.stringify(a) === JSON.stringify(b); }
    catch (e) { return false; }
  }

  function applyPayload(p) {
    const seq = ++state.payloadSeq;
    const decoded = decodePayload(p);
    // A slow decode of an older Streamlit fragment must never overwrite a
    // newer chart state. This was especially visible as BUY CE/BUY PE markers
    // appearing briefly and then disappearing on the next rerun.
    if (seq !== state.payloadSeq) return;
    // Decode failure is transient: keep the existing candles, VWAP, levels and
    // markers instead of feeding an empty dataset into Lightweight Charts.
    if (!decoded) return;
    const d = normalisePayload(decoded);
    if (!state.chart) return;

    syncSeries(state.candles, d.candles, "candle");
    if (state.vwap) {
      if (d.vwap.length) syncSeries(state.vwap, d.vwap, "vwap");
      else {
        state.vwap.setData([]);
        state.vwapData = [];
      }
    }

    // Do not remove/recreate price lines on every 1-second fragment rerun.
    // Recreating them was the main source of the visible chart blinking.
    if (!sameJson(state.lastLtp, d.ltp)) {
      if (d.ltp != null && Number.isFinite(d.ltp)) {
        if (state.ltpLine && typeof state.ltpLine.applyOptions === "function") {
          // Lightweight Charts exposes price-line mutation. Updating the
          // existing line avoids remove/create on every tick, which was the
          // remaining source of the visible yellow/grey-line blink.
          try {
            state.ltpLine.applyOptions({ price: d.ltp });
          } catch (e) {
            try { state.candles.removePriceLine(state.ltpLine); } catch (_) {}
            state.ltpLine = null;
          }
        }
        if (!state.ltpLine) {
          state.ltpLine = state.candles.createPriceLine({
            price: d.ltp,
            color: "#8b95a7",
            lineWidth: 1,
            lineStyle: 2,
            axisLabelVisible: true,
            title: "LTP",
          });
        }
      } else if (state.ltpLine) {
        try { state.candles.removePriceLine(state.ltpLine); } catch(e) {}
        state.ltpLine = null;
      }
      state.lastLtp = d.ltp;
    }

    if (!sameJson(state.lastLevels, d.levels)) {
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
      state.lastLevels = d.levels.slice();
    }

    // Support both Lightweight Charts marker APIs. Most importantly, keep the
    // marker timestamps tied to the candle series so an intrabar execution does
    // not silently disappear when its raw tick timestamp is not a bar timestamp.
    const candleTimes = (d.candles || []).map(x => Number(x.time)).filter(Number.isFinite);
    function snapMarkerTime(raw) {
      const t = Number(raw);
      if (!Number.isFinite(t) || !candleTimes.length) return null;
      let best = candleTimes[0];
      let bestDelta = Math.abs(best - t);
      for (let i = 1; i < candleTimes.length; i++) {
        const delta = Math.abs(candleTimes[i] - t);
        if (delta < bestDelta) { best = candleTimes[i]; bestDelta = delta; }
      }
      // Keep only plausible matches. Two candle widths covers live/replay
      // reconstruction differences without placing a marker on a random day.
      const step = candleTimes.length > 1
        ? Math.max(1, Math.abs(candleTimes[candleTimes.length - 1] - candleTimes[candleTimes.length - 2]))
        : 300;
      return bestDelta <= step * 2 ? best : null;
    }
    const markerData = (d.markers || [])
      .map(m => ({...m, time: snapMarkerTime(m.time)}))
      .filter(m => m.time != null)
      .sort((a, b) => Number(a.time) - Number(b.time));

    if (!sameJson(state.lastMarkers, markerData)) {
      try {
        if (typeof state.candles.setMarkers === "function") {
          state.candles.setMarkers(markerData);
        } else if (typeof LightweightCharts.createSeriesMarkers === "function") {
          if (state.markerPlugin && typeof state.markerPlugin.setMarkers === "function") {
            state.markerPlugin.setMarkers(markerData);
          } else {
            state.markerPlugin = LightweightCharts.createSeriesMarkers(state.candles, markerData);
          }
        }
        state.lastMarkers = markerData.slice();
      } catch (e) {
      // Never let marker rendering break the candle/chart itself.
      try {
        if (state.markerPlugin && typeof state.markerPlugin.setMarkers === "function") {
          state.markerPlugin.setMarkers(markerData);
          state.lastMarkers = markerData.slice();
        }
      } catch (_) {}
      }
    }

    const previous = state.lastPayload;
    const previousLast = previous && previous.candles && previous.candles.length
      ? Number(previous.candles[previous.candles.length - 1].time) : null;
    const incomingLast = d.candles.length
      ? Number(d.candles[d.candles.length - 1].time) : null;

    if (!state.hasFit || d.fitContent) {
      state.chart.timeScale().fitContent();
      state.hasFit = true;
    } else if (incomingLast != null && previousLast != null && incomingLast > previousLast) {
      // Live feeds append a new candle as the timeframe rolls over. setData()
      // updates the series but does not necessarily move an existing viewport.
      // Keep the live chart pinned to the newest candle when the candle time
      // actually advances. Replay/backtest uses fitContent=True and therefore
      // takes the branch above instead.
      state.chart.timeScale().scrollToRealTime();
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
        // Keep the real UTC epoch timestamps, but render all visible chart
        // labels in Indian Standard Time. Otherwise 09:15 IST appears as
        // 03:45 because Lightweight Charts formats numeric timestamps in UTC.
        tickMarkFormatter: (time, tickMarkType, locale) => {
          const d = new Date(Number(time) * 1000);
          if (Number.isNaN(d.getTime())) return "";
          return new Intl.DateTimeFormat("en-IN", {
            timeZone: "Asia/Kolkata",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }).format(d);
        },
      },
      localization: {
        locale: "en-IN",
        timeFormatter: (time) => {
          const d = new Date(Number(time) * 1000);
          if (Number.isNaN(d.getTime())) return "";
          return new Intl.DateTimeFormat("en-IN", {
            timeZone: "Asia/Kolkata",
            day: "2-digit",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          }).format(d) + " IST";
        },
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
    """Return a JSON-safe Lightweight Charts marker or None.

    Marker times produced by app.py are already Unix *seconds*.  Passing an
    integer Unix timestamp through pd.Timestamp(integer) interprets it as
    nanoseconds, which moves every marker to January 1970 and makes all
    BUY/SELL markers appear on the same candle/edge of the chart.  Preserve
    numeric timestamps as seconds and only parse actual datetime-like values.
    """
    if not isinstance(marker, dict):
        return None

    raw_time = marker.get("time")
    try:
        # bool is an int subclass but is never a valid chart timestamp.
        if isinstance(raw_time, bool):
            raise ValueError("boolean is not a timestamp")

        if isinstance(raw_time, (int, float)):
            if not math.isfinite(float(raw_time)):
                raise ValueError("non-finite timestamp")
            ts = int(float(raw_time))
        elif isinstance(raw_time, str) and raw_time.strip():
            text = raw_time.strip()
            # Numeric strings are also Unix seconds.
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
                ts = int(float(text))
            else:
                ts = int(pd.Timestamp(raw_time).timestamp())
        else:
            ts = int(pd.Timestamp(raw_time).timestamp())
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
    """Build a bounded JSON-safe chart payload.

    Live/realtime views use a rolling candle window instead of transmitting the
    entire historical month on every fragment rerun.  Historical/replay views
    can explicitly request the full dataset with ``fit_content=True``.
    """
    if df is None or df.empty:
        core = {
            "candles": [], "vwap": [], "ltp": _json_float(ltp),
            "levels": levels or [], "markers": [],
            "title": str(title), "height": int(height),
            "fitContent": bool(fit_content),
        }
    else:
        # ``max_candles=None`` is used by the app for both live and historical
        # chart calls.  A live call has fit_content=False, so keep only a bounded
        # rolling window.  Full historical/replay views set fit_content=True and
        # may intentionally send the complete loaded window.
        if max_candles is None:
            plot = df if fit_content else df.tail(720)
        else:
            plot = df.tail(max(1, int(max_candles)))

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
            try:
                style = int(level.get("style", 2))
            except (TypeError, ValueError):
                style = 2
            safe_levels.append({
                "price": price,
                "title": str(level.get("title") or ""),
                "color": str(level.get("color") or "#aab2bf"),
                "style": style,
            })

        safe_markers = []
        for marker in (markers or []):
            item = _normalise_marker(marker)
            if item is not None:
                safe_markers.append(item)

        core = {
            "candles": candles,
            "vwap": vw,
            "ltp": _json_float(ltp),
            "levels": safe_levels,
            "markers": safe_markers,
            "title": str(title),
            "height": int(height),
            "fitContent": bool(fit_content),
        }

    # Validate before handing data to the bidi transport.  Never emit NaN/Inf
    # because a single invalid value can corrupt the component payload.
    json.dumps(
        core,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return core


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
