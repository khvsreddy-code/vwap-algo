"""Persistent TradingView-style chart for the Streamlit terminal.

v9.4.41
The live chart previously sent a large gzip/base64 payload through the
Streamlit bidi component on every 1-second fragment rerun. That could produce
"BidiComponent Error: Unexpected end of input" and visible chart blinking.

This version:
- sends a bounded live candle window as ordinary JSON;
- keeps the browser-side Lightweight Charts instance alive;
- updates candles/lines/markers in place;
- never refits the live viewport on every tick;
- avoids the async gzip/decompression race.
"""

import json
import math
import re

import pandas as pd
import streamlit as st


LIVE_CANDLE_LIMIT = 720

_CHART_HTML = """
<div class="vwap-chart-host" data-vwap-host="1"></div>
"""

_CHART_CSS = """
:host {
  display:block;
  width:100%;
  height:100%;
  min-height:520px;
  background:#0b0f14;
  overflow:hidden;
}
.vwap-chart-host {
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
  let host = root.querySelector('[data-vwap-host="1"]');

  if (!host) {
    host = document.createElement("div");
    host.className = "vwap-chart-host";
    host.dataset.vwapHost = "1";
    root.appendChild(host);
  }

  const state = host.__vwapState || {
    chart:null,
    candles:null,
    vwap:null,
    ltpLine:null,
    levelLines:[],
    markerPlugin:null,
    lastCandles:[],
    lastVwap:[],
    lastLtp:null,
    lastLevels:[],
    lastMarkers:[],
    previousLastTime:null,
    ready:false,
    loading:false,
    loadPromise:null,
    fitted:false,
    resizeObserver:null
  };
  host.__vwapState = state;

  function loadScript() {
    if (window.LightweightCharts) return Promise.resolve();
    if (state.loading && state.loadPromise) return state.loadPromise;

    state.loading = true;
    state.loadPromise = new Promise((resolve,reject) => {
      const existing = document.querySelector('script[data-vwap-lwc="1"]');
      if (existing) {
        if (window.LightweightCharts) {
          resolve();
          return;
        }
        existing.addEventListener("load", () => resolve(), {once:true});
        existing.addEventListener("error", reject, {once:true});
        return;
      }

      const script = document.createElement("script");
      script.dataset.vwapLwc = "1";
      script.src =
        "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js";
      script.onload = () => resolve();
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return state.loadPromise;
  }

  function rowEqual(a,b) {
    if (!a || !b || Number(a.time) !== Number(b.time)) return false;
    for (const k of ["open","high","low","close","value"]) {
      if ((a[k] == null) !== (b[k] == null)) return false;
      if (a[k] != null && Number(a[k]) !== Number(b[k])) return false;
    }
    return true;
  }

  function arraysEqual(a,b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    if (!a.length) return true;
    if (!rowEqual(a[0],b[0]) || !rowEqual(a[a.length-1],b[b.length-1])) return false;
    for (let i=1; i<a.length-1; i++) {
      if (!rowEqual(a[i],b[i])) return false;
    }
    return true;
  }

  function jsonEqual(a,b) {
    try { return JSON.stringify(a) === JSON.stringify(b); }
    catch (_) { return false; }
  }

  function syncSeries(series, incoming, key) {
    const next = Array.isArray(incoming) ? incoming : [];
    const previous = state[key] || [];

    if (arraysEqual(previous,next)) return false;

    if (
      previous.length &&
      previous.length === next.length &&
      Number(previous[0].time) === Number(next[0].time)
    ) {
      const last = next[next.length-1];
      if (last && !rowEqual(previous[previous.length-1],last)) {
        try {
          series.update(last);
          state[key] = next.slice();
          return true;
        } catch (_) {}
      }
    }

    series.setData(next);
    state[key] = next.slice();
    return true;
  }

  function snapMarkerTime(raw,times) {
    const t = Number(raw);
    if (!Number.isFinite(t) || !times.length) return null;

    let best = times[0];
    let delta = Math.abs(best-t);

    for (let i=1; i<times.length; i++) {
      const d = Math.abs(times[i]-t);
      if (d < delta) {
        best = times[i];
        delta = d;
      }
    }

    const step = times.length > 1
      ? Math.max(1,Math.abs(times[times.length-1]-times[times.length-2]))
      : 300;

    return delta <= step*2 ? best : null;
  }

  function applyLtp(value) {
    if (jsonEqual(state.lastLtp,value)) return;

    if (value != null && Number.isFinite(value)) {
      if (state.ltpLine && state.ltpLine.applyOptions) {
        try {
          state.ltpLine.applyOptions({price:value});
        } catch (_) {
          try { state.candles.removePriceLine(state.ltpLine); } catch (_) {}
          state.ltpLine = null;
        }
      }

      if (!state.ltpLine) {
        state.ltpLine = state.candles.createPriceLine({
          price:value,
          color:"#8b95a7",
          lineWidth:1,
          lineStyle:2,
          axisLabelVisible:true,
          title:"LTP"
        });
      }
    } else if (state.ltpLine) {
      try { state.candles.removePriceLine(state.ltpLine); } catch (_) {}
      state.ltpLine = null;
    }

    state.lastLtp = value;
  }

  function applyLevels(levels) {
    if (jsonEqual(state.lastLevels,levels)) return;

    for (const line of state.levelLines) {
      try { state.candles.removePriceLine(line); } catch (_) {}
    }
    state.levelLines = [];

    for (const x of levels) {
      const price = Number(x && x.price);
      if (!Number.isFinite(price)) continue;

      state.levelLines.push(
        state.candles.createPriceLine({
          price,
          color:String(x.color || "#aab2bf"),
          lineWidth:1,
          lineStyle:Number(x.style || 2),
          axisLabelVisible:true,
          title:String(x.title || "")
        })
      );
    }

    state.lastLevels = levels.slice();
  }

  function applyMarkers(markers,candles) {
    const times = candles.map(x => Number(x.time)).filter(Number.isFinite);

    const next = markers
      .map(m => {
        const t = snapMarkerTime(m && m.time,times);
        if (t == null) return null;
        return {
          ...m,
          time:t,
          position:m.position || "inBar",
          shape:m.shape || "circle",
          text:m.text || ""
        };
      })
      .filter(Boolean)
      .sort((a,b) => Number(a.time)-Number(b.time));

    if (jsonEqual(state.lastMarkers,next)) return;

    try {
      if (typeof state.candles.setMarkers === "function") {
        state.candles.setMarkers(next);
      } else if (typeof LightweightCharts.createSeriesMarkers === "function") {
        if (!state.markerPlugin) {
          state.markerPlugin =
            LightweightCharts.createSeriesMarkers(state.candles,next);
        } else if (state.markerPlugin.setMarkers) {
          state.markerPlugin.setMarkers(next);
        }
      }
      state.lastMarkers = next.slice();
    } catch (err) {
      console.warn("VWAP marker update failed:",err);
    }
  }

  function applyPayload(input) {
    if (!state.chart) return;

    const p = input && typeof input === "object" ? input : {};
    const candles = Array.isArray(p.candles) ? p.candles : [];
    const vwap = Array.isArray(p.vwap) ? p.vwap : [];
    const levels = Array.isArray(p.levels) ? p.levels : [];
    const markers = Array.isArray(p.markers) ? p.markers : [];
    const ltp = p.ltp == null ? null : Number(p.ltp);
    const fitContent = Boolean(p.fitContent);

    const candleChanged = syncSeries(state.candles,candles,"lastCandles");

    if (vwap.length) {
      syncSeries(state.vwap,vwap,"lastVwap");
    } else if (state.lastVwap.length) {
      state.vwap.setData([]);
      state.lastVwap = [];
    }

    applyLtp(ltp);
    applyLevels(levels);
    applyMarkers(markers,candles);

    const incomingLast =
      candles.length ? Number(candles[candles.length-1].time) : null;

    // Never refit a live chart every second. Re-fitting was one of the main
    // visual causes of the chart jumping/blinking.
    if (!state.fitted || (fitContent && candleChanged)) {
      state.chart.timeScale().fitContent();
      state.fitted = true;
    } else if (
      candleChanged &&
      incomingLast != null &&
      state.previousLastTime != null &&
      incomingLast > state.previousLastTime
    ) {
      state.chart.timeScale().scrollToRealTime();
    }

    state.previousLastTime = incomingLast;
  }

  function init() {
    if (state.ready) {
      applyPayload(data);
      return;
    }

    const L = window.LightweightCharts;

    state.chart = L.createChart(host,{
      width:Math.max(320,host.clientWidth || 900),
      height:Math.max(200,host.clientHeight || data.height || 620),
      layout:{
        background:{type:"solid",color:"#0b0f14"},
        textColor:"#9aa4b2"
      },
      grid:{
        vertLines:{color:"#17202b"},
        horzLines:{color:"#17202b"}
      },
      crosshair:{mode:L.CrosshairMode.Normal},
      rightPriceScale:{borderColor:"#263241",autoScale:true},
      timeScale:{
        borderColor:"#263241",
        timeVisible:true,
        secondsVisible:false,
        rightOffset:8,
        barSpacing:8,
        lockVisibleTimeRangeOnResize:true,
        tickMarkFormatter:(time) => {
          const d = new Date(Number(time)*1000);
          if (Number.isNaN(d.getTime())) return "";
          return new Intl.DateTimeFormat("en-IN",{
            timeZone:"Asia/Kolkata",
            hour:"2-digit",
            minute:"2-digit",
            hour12:false
          }).format(d);
        }
      },
      localization:{
        locale:"en-IN",
        timeFormatter:(time) => {
          const d = new Date(Number(time)*1000);
          if (Number.isNaN(d.getTime())) return "";
          return new Intl.DateTimeFormat("en-IN",{
            timeZone:"Asia/Kolkata",
            day:"2-digit",
            month:"short",
            hour:"2-digit",
            minute:"2-digit",
            hour12:false
          }).format(d) + " IST";
        }
      },
      handleScroll:{
        mouseWheel:true,
        pressedMouseMove:true,
        horzTouchDrag:true,
        vertTouchDrag:true
      },
      handleScale:{
        mouseWheel:true,
        pinch:true,
        axisPressedMouseMove:true
      }
    });

    state.candles = state.chart.addCandlestickSeries({
      upColor:"#22b39b",
      downColor:"#ef5350",
      borderVisible:false,
      wickUpColor:"#22b39b",
      wickDownColor:"#ef5350",
      priceLineVisible:false,
      lastValueVisible:true
    });

    state.vwap = state.chart.addLineSeries({
      color:"#4aa3ff",
      lineWidth:2,
      priceLineVisible:false,
      lastValueVisible:true
    });

    state.resizeObserver = new ResizeObserver(() => {
      if (!state.chart) return;
      const w = host.clientWidth;
      const h = host.clientHeight;
      if (w > 10 && h > 10) {
        state.chart.applyOptions({width:w,height:h});
      }
    });
    state.resizeObserver.observe(host);

    state.ready = true;
    applyPayload(data);
  }

  loadScript().then(init).catch((err) => {
    console.error("VWAP chart library load failed:",err);
    host.textContent = "Unable to load the chart library.";
  });

  // Do not destroy the chart on a normal Streamlit fragment rerun.
  return () => {};
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
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _normalise_marker(marker):
    if not isinstance(marker,dict):
        return None

    raw_time = marker.get("time")

    try:
        if isinstance(raw_time,bool):
            raise ValueError("boolean timestamp")

        if isinstance(raw_time,(int,float)):
            if not math.isfinite(float(raw_time)):
                raise ValueError("non-finite timestamp")
            ts = int(float(raw_time))
        elif isinstance(raw_time,str) and raw_time.strip():
            text = raw_time.strip()
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?",text):
                ts = int(float(text))
            else:
                ts = int(pd.Timestamp(raw_time).timestamp())
        else:
            ts = int(pd.Timestamp(raw_time).timestamp())
    except (TypeError,ValueError,OverflowError):
        return None

    position = str(marker.get("position") or "")
    if position not in {"aboveBar","belowBar","inBar"}:
        position = "inBar"

    shape = str(marker.get("shape") or "")
    if shape not in {"arrowUp","arrowDown","circle","square"}:
        shape = "circle"

    out = {
        "time":ts,
        "position":position,
        "shape":shape,
        "text":str(marker.get("text") or "")
    }

    if marker.get("color"):
        out["color"] = str(marker["color"])

    if marker.get("size") is not None:
        try:
            out["size"] = max(1,int(marker["size"]))
        except (TypeError,ValueError):
            pass

    return out


def make_payload(
    df,
    vwap=True,
    ltp=None,
    levels=None,
    markers=None,
    title="",
    height=900,
    max_candles=90,
    fit_content=False,
):
    """Build a bounded JSON-safe payload.

    For live views, ``max_candles=None`` is intentionally capped to a rolling
    window. Full historical/replay views can still send all rows when they use
    ``fit_content=True``.
    """
    if df is None or df.empty:
        plot = pd.DataFrame()
    elif max_candles is None:
        plot = df if fit_content else df.tail(LIVE_CANDLE_LIMIT)
    else:
        plot = df.tail(max(1,int(max_candles)))

    candles = []
    vw = []

    if not plot.empty:
        for r in plot.itertuples(index=False):
            try:
                ts = int(pd.Timestamp(getattr(r,"datetime")).timestamp())
            except (TypeError,ValueError,OverflowError):
                continue

            o = _json_float(getattr(r,"open",None))
            h = _json_float(getattr(r,"high",None))
            lo = _json_float(getattr(r,"low",None))
            c = _json_float(getattr(r,"close",None))

            if None in (o,h,lo,c):
                continue

            candles.append({
                "time":ts,
                "open":o,
                "high":h,
                "low":lo,
                "close":c,
            })

            if vwap and hasattr(r,"vwap"):
                vv = _json_float(getattr(r,"vwap"))
                if vv is not None:
                    vw.append({"time":ts,"value":vv})

    safe_levels = []
    for level in levels or []:
        if not isinstance(level,dict):
            continue

        price = _json_float(level.get("price"))
        if price is None:
            continue

        try:
            style = int(level.get("style",2))
        except (TypeError,ValueError):
            style = 2

        safe_levels.append({
            "price":price,
            "title":str(level.get("title") or ""),
            "color":str(level.get("color") or "#aab2bf"),
            "style":style,
        })

    safe_markers = []
    for marker in markers or []:
        item = _normalise_marker(marker)
        if item is not None:
            safe_markers.append(item)

    payload = {
        "candles":candles,
        "vwap":vw,
        "ltp":_json_float(ltp),
        "levels":safe_levels,
        "markers":safe_markers,
        "title":str(title),
        "height":int(height),
        "fitContent":bool(fit_content),
    }

    # Validate before sending anything through the bidi transport.
    json.dumps(
        payload,
        separators=(",",":"),
        ensure_ascii=False,
        allow_nan=False,
    )

    return payload


def render(
    df,
    title,
    height=900,
    vwap=True,
    ltp=None,
    levels=None,
    markers=None,
    max_candles=90,
    fit_content=False,
):
    payload = make_payload(
        df,
        vwap=vwap,
        ltp=ltp,
        levels=levels,
        markers=markers,
        title=title,
        height=height,
        max_candles=max_candles,
        fit_content=fit_content,
    )

    if _component is None:
        st.error(
            "This live chart requires Streamlit Custom Components V2. "
            "Upgrade Streamlit to 1.52+."
        )
        return

    # Stable key: the chart is one browser component, not a new chart per tick.
    component_key = f"live-{title}-{int(height)}"

    try:
        _component(
            data=payload,
            key=component_key,
            width="stretch",
            height=int(height),
        )
    except TypeError as exc:
        if (
            "width" in str(exc)
            or "height" in str(exc)
            or "unexpected keyword" in str(exc)
        ):
            _component(data=payload,key=component_key)
        else:
            raise
