
"""Persistent TradingView-style chart for the Streamlit terminal.

v9.4.52 — persistent DOM chart + true incremental updates.

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
<div id="vwap-live-chart" class="vwap-chart-shell">
  <canvas class="vwap-chart-canvas" aria-label="VWAP live chart"></canvas>
  <div class="vwap-chart-hint">Scroll to zoom • drag to pan</div>
</div>
"""

_CHART_CSS = """
:host {
  display:block;
  width:100%;
  height:100%;
  min-height:0;
  margin:0;
  padding:0;
  box-sizing:border-box;
  overflow:hidden;
  background:#0a0f16;
}
.vwap-chart-shell {
  position:relative;
  display:block;
  width:100%;
  height:100%;
  min-height:0;
  margin:0;
  padding:0;
  box-sizing:border-box;
  overflow:hidden;
  border:1px solid #1d2836;
  border-radius:12px;
  background:linear-gradient(180deg,#0b1119 0%,#090e15 100%);
}
.vwap-chart-canvas {
  position:absolute;
  inset:0;
  display:block;
  width:100%;
  height:100%;
}
.vwap-chart-hint {
  position:absolute;
  right:10px;
  bottom:8px;
  z-index:2;
  padding:4px 8px;
  border:1px solid rgba(100,116,139,.28);
  border-radius:999px;
  background:rgba(9,14,21,.76);
  color:#718096;
  font:11px/1.2 Inter,system-ui,sans-serif;
  pointer-events:none;
}
"""

_CHART_JS = r"""
export default function(component) {
  const root = component.parentElement || component;
  const data = component.data || {};
  const requestedHeight = Math.max(260, Number(data.height || 620));
  try {
    root.style.width = "100%";
    root.style.height = requestedHeight + "px";
    root.style.minHeight = requestedHeight + "px";
    root.style.maxHeight = requestedHeight + "px";
    root.style.boxSizing = "border-box";
    root.style.overflow = "hidden";
  } catch (e) {}

  // IMPORTANT: Streamlit can recreate the component wrapper during a fragment
  // rerun even when the component key is unchanged. Keep the actual chart DOM
  // node in a window-level registry and move that SAME node into the new
  // wrapper. Lightweight Charts therefore keeps the same canvas/series/chart
  // instance instead of destroying and rebuilding it.
  const registry = window.__fyersVwapCharts || (window.__fyersVwapCharts = {});
  const chartKey = String(data.componentKey || component.key || data.title || "default");
  let state = registry[chartKey];

  if (!state) {
    state = {
      host: null,
      chart: null,
      candles: null,
      vwap: null,
      ltpLine: null,
      levelLines: [],
      ready: false,
      loading: false,
      loadPromise: null,
      lastPayload: null,
      resizeObserver: null,
      candleData: [],
      vwapData: [],
      lastMarkers: [],
      lastLevels: [],
      lastLtp: null,
      hasFit: false,
      payloadSeq: 0,
      fallback: false,
      fallbackCanvas: null,
      fallbackCtx: null,
      fallbackData: null,
      fallbackView: { start: 0, count: 0 },
      fallbackDrag: null,
    };
    registry[chartKey] = state;
  }

  let host = state.host;
  if (!host || !host.isConnected) {
    host = document.createElement("div");
    host.id = "vwap-live-chart";
    host.style.width = "100%";
    host.style.height = requestedHeight + "px";
    host.style.minHeight = requestedHeight + "px";
    host.style.maxHeight = requestedHeight + "px";
    host.style.boxSizing = "border-box";
    host.style.background = "#0b0f14";
    state.host = host;
  }
  // Move, don't recreate, the existing chart node. This is the key to
  // eliminating visible blink during Streamlit fragment reruns.
  if (host.parentElement !== root) root.appendChild(host);
  host.style.width = "100%";
  host.style.height = requestedHeight + "px";
  host.style.minHeight = requestedHeight + "px";
  host.style.maxHeight = requestedHeight + "px";
  host.style.boxSizing = "border-box";

  function loadScript() {
    if (window.LightweightCharts) return Promise.resolve();
    if (state.loading && state.loadPromise) return state.loadPromise;
    state.loading = true;
    state.loadPromise = new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-vwap-lwc="1"]');
      if (existing) {
        // The script tag can survive a component remount after it has already
        // loaded. In that case a new "load" listener will never fire.
        if (window.LightweightCharts) {
          resolve();
          return;
        }
        existing.addEventListener("load", () => resolve(), { once: true });
        existing.addEventListener("error", reject, { once: true });
        return;
      }
      const script = document.createElement("script");
      script.dataset.vwapLwc = "1";
      // The canvas renderer below is the deployment-safe fallback. CDN loading
      // is only an optional enhancement; Streamlit Cloud/network policies must
      // never be able to make the chart disappear.
      const urls = [
        "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js",
        "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"
      ];
      let attempt = 0;
      const tryNext = () => {
        if (window.LightweightCharts) return resolve();
        if (attempt >= urls.length) return reject(new Error("chart library unavailable"));
        const tag = attempt++ === 0 ? script : document.createElement("script");
        tag.dataset.vwapLwc = "1";
        tag.src = urls[attempt - 1];
        tag.onload = () => resolve();
        tag.onerror = tryNext;
        if (!tag.parentNode) document.head.appendChild(tag);
      };
      tryNext();
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
      mode: p.mode || "full",
      candles: Array.isArray(p.candles) ? p.candles : [],
      candle: p.candle || null,
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

    // Always retain the latest complete payload for the deployment-safe canvas
    // renderer. This gives us a recovery path even when the browser-side
    // Lightweight Charts runtime loads but fails during a series operation.
    if (d.mode !== "delta") {
      state.fallbackData = d;
    }

    // The dependency-free canvas renderer has no Lightweight Charts instance.
    // It must receive every rerun payload too; otherwise a CDN/runtime failure
    // leaves the fallback stuck on its initial empty state.
    if (state.fallback && !state.chart) {
      if (d.mode === "delta") {
        const prior = state.fallbackData || {
          candles: [], vwap: [], levels: [], markers: [],
          ltp: null, title: d.title || "", height: d.height || requestedHeight,
          fitContent: false
        };
        const candles = Array.isArray(prior.candles) ? prior.candles.slice() : [];
        const vw = Array.isArray(prior.vwap) ? prior.vwap.slice() : [];
        if (d.candle) {
          const idx = candles.findIndex(x => Number(x.time) === Number(d.candle.time));
          if (idx >= 0) candles[idx] = d.candle;
          else candles.push(d.candle);
          candles.sort((a,b) => Number(a.time) - Number(b.time));
          while (candles.length > 180) candles.shift();
        }
        if (d.vwap) {
          const idx = vw.findIndex(x => Number(x.time) === Number(d.vwap.time));
          if (idx >= 0) vw[idx] = d.vwap;
          else vw.push(d.vwap);
          vw.sort((a,b) => Number(a.time) - Number(b.time));
          while (vw.length > 180) vw.shift();
        }
        state.fallbackData = {
          ...prior, mode: "full", candles, vwap: vw,
          ltp: d.ltp, levels: d.levels || prior.levels,
          markers: d.markers || prior.markers,
          title: d.title || prior.title, height: d.height || prior.height
        };
      } else {
        state.fallbackData = d;
      }
      drawFallback();
      state.lastPayload = d;
      return;
    }
    if (!state.chart) return;

    if (d.mode === "delta") {
      if (d.candle && state.candles) {
        try { state.candles.update(d.candle); } catch (e) {}
        state.candleData = state.candleData || [];
        if (state.candleData.length &&
            Number(state.candleData[state.candleData.length - 1].time) === Number(d.candle.time)) {
          state.candleData[state.candleData.length - 1] = d.candle;
        } else {
          state.candleData.push(d.candle);
        }
      }
      if (state.vwap && d.vwap) {
        try { state.vwap.update(d.vwap); } catch (e) {}
        state.vwapData = state.vwapData || [];
        if (state.vwapData.length &&
            Number(state.vwapData[state.vwapData.length - 1].time) === Number(d.vwap.time)) {
          state.vwapData[state.vwapData.length - 1] = d.vwap;
        } else {
          state.vwapData.push(d.vwap);
        }
      }
    } else {
      try {
        syncSeries(state.candles, d.candles, "candle");
      } catch (e) {
        // Keep the terminal visible if a browser-side chart API mismatch occurs.
        initFallback();
        state.fallbackData = d;
        drawFallback();
        return;
      }
      if (state.vwap) {
        if (d.vwap.length) syncSeries(state.vwap, d.vwap, "vwap");
        else {
          state.vwap.setData([]);
          state.vwapData = [];
        }
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
    const markerCandles = d.mode === "delta" ? (state.candleData || []) : (d.candles || []);
    const candleTimes = markerCandles.map(x => Number(x.time)).filter(Number.isFinite);
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
    const previousLast = previous && previous.mode === "delta"
      ? Number(previous.candle && previous.candle.time)
      : (previous && previous.candles && previous.candles.length
          ? Number(previous.candles[previous.candles.length - 1].time) : null);
    const incomingLast = d.mode === "delta"
      ? Number(d.candle && d.candle.time)
      : (d.candles.length ? Number(d.candles[d.candles.length - 1].time) : null);

    if (d.mode !== "delta" && (!state.hasFit || d.fitContent)) {
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


  // Dependency-free Streamlit renderer. This is intentionally self-contained:
  // if an external chart library is blocked by CSP, ad-blocking, offline
  // deploys, or a corporate proxy, the chart still renders with candles, VWAP,
  // levels, LTP and BUY CE/BUY PE labels.
  function initFallback() {
    state.fallback = true;
    const canvas = host.querySelector(".vwap-chart-canvas") || document.createElement("canvas");
    if (!canvas.parentElement) host.appendChild(canvas);
    state.fallbackCanvas = canvas;
    state.fallbackCtx = canvas.getContext("2d");
    const hint = host.querySelector(".vwap-chart-hint");
    if (hint) hint.style.display = "block";

    if (!state.fallbackBound) {
      state.fallbackBound = true;
      canvas.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const d = state.fallbackData || {};
        const n = (d.candles || []).length;
        if (!n) return;
        const current = state.fallbackView.count || Math.min(120, n);
        const factor = ev.deltaY > 0 ? 1.16 : 0.86;
        const next = Math.max(25, Math.min(n, Math.round(current * factor)));
        const ratio = next / current;
        const anchor = Math.max(0, Math.min(n - 1, Math.round((state.fallbackView.start || 0) + current * ((ev.offsetX || canvas.clientWidth * .7) / Math.max(1, canvas.clientWidth)))));
        state.fallbackView.count = next;
        state.fallbackView.start = Math.max(0, Math.min(n-next, Math.round(anchor - next/ratio * ((ev.offsetX || canvas.clientWidth*.7) / Math.max(1,canvas.clientWidth)))));
        drawFallback();
      }, {passive:false});
      canvas.addEventListener("pointerdown", ev => {
        canvas.setPointerCapture(ev.pointerId);
        state.fallbackDrag = { x: ev.clientX, start: state.fallbackView.start || 0 };
      });
      canvas.addEventListener("pointermove", ev => {
        if (!state.fallbackDrag) return;
        const d = state.fallbackData || {}, n=(d.candles||[]).length;
        const count=state.fallbackView.count || Math.min(120,n);
        const dx=ev.clientX-state.fallbackDrag.x;
        const px=Math.max(1,canvas.clientWidth);
        const shift=Math.round(dx/px*count);
        state.fallbackView.start=Math.max(0,Math.min(Math.max(0,n-count),state.fallbackDrag.start-shift));
        drawFallback();
      });
      canvas.addEventListener("pointerup", () => state.fallbackDrag=null);
      canvas.addEventListener("pointercancel", () => state.fallbackDrag=null);
    }
    resizeFallback();
    drawFallback();
  }

  function resizeFallback() {
    const canvas=state.fallbackCanvas;
    if (!canvas) return;
    const r=canvas.getBoundingClientRect();
    const dpr=window.devicePixelRatio || 1;
    const w=Math.max(320,Math.round(r.width)), h=Math.max(260,Math.round(r.height));
    if (canvas.width!==Math.round(w*dpr) || canvas.height!==Math.round(h*dpr)) {
      canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr);
      state.fallbackCtx.setTransform(dpr,0,0,dpr,0,0);
    }
  }

  function drawFallback() {
    const ctx=state.fallbackCtx, canvas=state.fallbackCanvas, d=state.fallbackData||{};
    if (!ctx||!canvas) return;
    resizeFallback();
    const W=canvas.clientWidth||900,H=canvas.clientHeight||600;
    ctx.clearRect(0,0,W,H);
    ctx.fillStyle="#0a0f16"; ctx.fillRect(0,0,W,H);
    const candles=(d.candles||[]).filter(x=>Number.isFinite(Number(x.close)));
    if (!candles.length) {
      ctx.fillStyle="#7f8b9b"; ctx.font="13px system-ui";
      ctx.fillText("Waiting for market data…",24,32); return;
    }
    const n=candles.length;
    const count=Math.max(1,Math.min(n,state.fallbackView.count||Math.min(120,n)));
    const start=Math.max(0,Math.min(n-count,state.fallbackView.start||Math.max(0,n-count)));
    state.fallbackView.count=count; state.fallbackView.start=start;
    const view=candles.slice(start,start+count);
    const vals=[];
    view.forEach(c=>{["open","high","low","close"].forEach(k=>{const v=Number(c[k]);if(Number.isFinite(v))vals.push(v);});});
    (d.vwap||[]).slice(start,start+count).forEach(x=>{const v=Number(x.value);if(Number.isFinite(v))vals.push(v);});
    (d.levels||[]).forEach(x=>{const v=Number(x.price);if(Number.isFinite(v))vals.push(v);});
    if (d.ltp!=null && Number.isFinite(Number(d.ltp))) vals.push(Number(d.ltp));
    let lo=Math.min(...vals),hi=Math.max(...vals); const pad=Math.max((hi-lo)*.08,.5);lo-=pad;hi+=pad;
    const L=62,R=78,T=28,B=36,plotW=Math.max(1,W-L-R),plotH=Math.max(1,H-T-B);
    const y=v=>T+(hi-v)/(hi-lo)*plotH;
    const x=i=>L+(i+.5)*plotW/view.length;
    ctx.strokeStyle="#182230";ctx.lineWidth=1;
    for(let g=0;g<=5;g++){const yy=T+g*plotH/5;ctx.beginPath();ctx.moveTo(L,yy);ctx.lineTo(W-R,yy);ctx.stroke();
      const price=hi-(hi-lo)*g/5;ctx.fillStyle="#718096";ctx.font="11px system-ui";ctx.fillText(price.toFixed(2),W-R+8,yy+4);}
    const step=plotW/view.length, body=Math.max(2,Math.min(10,step*.62));
    view.forEach((c,i)=>{
      const o=Number(c.open),h=Number(c.high),l=Number(c.low),cl=Number(c.close),xx=x(i),up=cl>=o;
      ctx.strokeStyle=up?"#27c49a":"#f05d63";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(xx,y(h));ctx.lineTo(xx,y(l));ctx.stroke();
      ctx.fillStyle=up?"#27c49a":"#f05d63";const top=y(Math.max(o,cl)),bot=y(Math.min(o,cl));ctx.fillRect(xx-body/2,top,body,Math.max(1,bot-top));
    });
    const vwap=(d.vwap||[]).slice(start,start+count);
    if(vwap.length){ctx.strokeStyle="#55a8ff";ctx.lineWidth=2;ctx.beginPath();vwap.forEach((q,i)=>{const vv=Number(q.value);if(!Number.isFinite(vv))return;const xx=x(i),yy=y(vv);if(i===0)ctx.moveTo(xx,yy);else ctx.lineTo(xx,yy);});ctx.stroke();}
    (d.levels||[]).forEach(q=>{const vv=Number(q.price);if(!Number.isFinite(vv))return;const yy=y(vv);ctx.setLineDash([5,4]);ctx.strokeStyle=q.color||"#8792a2";ctx.beginPath();ctx.moveTo(L,yy);ctx.lineTo(W-R,yy);ctx.stroke();ctx.setLineDash([]);});
    if(d.ltp!=null&&Number.isFinite(Number(d.ltp))){const yy=y(Number(d.ltp));ctx.setLineDash([3,3]);ctx.strokeStyle="#a6b1c2";ctx.beginPath();ctx.moveTo(L,yy);ctx.lineTo(W-R,yy);ctx.stroke();ctx.setLineDash([]);}
    const candleTimes=view.map(c=>Number(c.time));
    (d.markers||[]).forEach(m=>{
      const mt=Number(m.time);let best=-1,bd=Infinity;
      candleTimes.forEach((t,i)=>{const z=Math.abs(t-mt);if(z<bd){bd=z;best=i;}});
      if(best<0)return;
      const xx=x(best), yy=y(Number(view[best].high))+ (m.position==="belowBar"?24:-24);
      const isBuy=String(m.text||"").toUpperCase().startsWith("BUY");
      ctx.fillStyle=isBuy?"#1fc68f":"#f05d63";ctx.beginPath();ctx.arc(xx,yy,4,0,Math.PI*2);ctx.fill();
      const label=String(m.text||"").slice(0,14);ctx.font="bold 11px system-ui";const tw=ctx.measureText(label).width+12;
      const ly=m.position==="belowBar"?yy+8:yy-22;ctx.fillStyle=isBuy?"rgba(24,111,84,.96)":"rgba(129,38,47,.96)";
      ctx.roundRect(xx-tw/2,ly,tw,18,5);ctx.fill();ctx.fillStyle="#fff";ctx.fillText(label,xx-tw/2+6,ly+13);
    });
    ctx.fillStyle="#8b95a7";ctx.font="bold 12px system-ui";ctx.fillText(String(d.title||"VWAP"),L,18);
    ctx.fillStyle="#66758a";ctx.font="11px system-ui";
    const first=view[0],last=view[view.length-1];
    if(first?.time){ctx.fillText(new Date(Number(first.time)*1000).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Kolkata"}),L,H-10);}
    if(last?.time){const tx=new Date(Number(last.time)*1000).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Kolkata"});ctx.fillText(tx,W-R-45,H-10);}
  }

  function init() {
    if (state.ready) {
      applyPayload(data);
      return;
    }
    state.chart = LightweightCharts.createChart(host, {
      width: Math.max(320, Math.round(host.getBoundingClientRect().width || root.getBoundingClientRect().width || 900)),
      height: requestedHeight,
      autoSize: false,
      layout: {
        background: { type: "solid", color: "#0b0f14" },
        textColor: "#9aa4b2",
      },
      grid: {
        vertLines: { color: "#17202b" },
        horzLines: { color: "#17202b" },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: {
        borderColor: "#263241",
        autoScale: true,
        scaleMargins: { top: 0.14, bottom: 0.14 },
      },
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

    const resizeChart = () => {
      if (state.fallback) { resizeFallback(); drawFallback(); return; }
      if (!state.chart) return;
      const rect = host.getBoundingClientRect();
      const parentRect = root.getBoundingClientRect();
      const w = Math.max(320, Math.round(rect.width || parentRect.width || 900));
      const h = Math.max(260, Math.round(rect.height || requestedHeight));
      try { state.chart.applyOptions({ width: w, height: h }); } catch (e) {}
    };
    state.resizeObserver = new ResizeObserver(resizeChart);
    state.resizeObserver.observe(root);
    state.resizeObserver.observe(host);
    state.ready = true;
    requestAnimationFrame(() => { resizeChart(); applyPayload(data); });
  }

  loadScript().then(init).catch(() => {
    // Never show a dead chart in Streamlit Cloud. Fall back to the bundled,
    // dependency-free canvas renderer.
    initFallback();
    state.fallbackData = normalisePayload(data);
    state.lastPayload = state.fallbackData;
    drawFallback();
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
        name="fyers_vwap_live_chart_v9_4_59",
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
    height=900, max_candles=180, fit_content=False, incremental=False
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
            "componentKey": f"live-{title}-{int(height)}",
            "fitContent": bool(fit_content),
        }
    else:
        # ``max_candles=None`` is used by the app for both live and historical
        # chart calls.  A live call has fit_content=False, so keep only a bounded
        # rolling window.  Full historical/replay views set fit_content=True and
        # may intentionally send the complete loaded window.
        if max_candles is None:
            plot = df if fit_content else df.tail(180)
        else:
            plot = df.tail(max(1, int(max_candles)))

        candles = []
        vw = []
        # Lightweight Charts rejects a series containing duplicate or
        # out-of-order timestamps. Normalize the transport here as a final
        # safety net because live overlays can occasionally touch the same
        # timeframe bucket as REST history.
        by_time = {}
        vwap_by_time = {}
        for r in plot.sort_values("datetime").itertuples(index=False):
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

            by_time[ts] = {
                "time": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
            }

            if vwap and hasattr(r, "vwap"):
                vv = _json_float(getattr(r, "vwap"))
                if vv is not None:
                    vwap_by_time[ts] = {"time": ts, "value": vv}

        candles = [by_time[t] for t in sorted(by_time)]
        vw = [vwap_by_time[t] for t in sorted(vwap_by_time)]

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
        visible_start = candles[0]["time"] if candles else None
        visible_end = candles[-1]["time"] if candles else None
        for marker in (markers or []):
            item = _normalise_marker(marker)
            if item is None:
                continue
            # Historical markers outside the visible rolling window do not help
            # the live terminal and cannot be snapped to a visible candle.
            if visible_start is not None and visible_end is not None:
                if item["time"] < visible_start or item["time"] > visible_end:
                    continue
            safe_markers.append(item)

        core = {
            "candles": candles,
            "vwap": vw,
            "ltp": _json_float(ltp),
            "levels": safe_levels,
            "markers": safe_markers,
            "title": str(title),
            "height": int(height),
            "componentKey": f"live-{title}-{int(height)}",
            "fitContent": bool(fit_content),
        }

    # Live Terminal updates are incremental. Send full history once, then only
    # the current candle/VWAP point plus the small marker/level state.
    if incremental and not fit_content and core.get("candles"):
        chart_key = f"v9_4_52::{title}::{height}"
        signature = (
            core["candles"][0]["time"],
            core["candles"][-1]["time"],
            len(core["candles"]),
        )
        previous = st.session_state.get("_v952_chart_signature", {}).get(chart_key)
        st.session_state.setdefault("_v952_chart_signature", {})[chart_key] = signature

        # A normal tick changes only the last candle: send a tiny delta.
        # When a new candle rolls in, the rolling 180-bar window may drop its
        # oldest candle. Lightweight Charts has no cheap "remove first row"
        # operation, so send the bounded window once at the rollover. This
        # happens once per timeframe candle, not on every tick.
        can_delta = (
            previous is not None
            and previous[0] == signature[0]
            and previous[2] == signature[2]
        )
        if can_delta:
            core = {
                "mode": "delta",
                "candle": core["candles"][-1],
                "vwap": core["vwap"][-1] if core.get("vwap") else None,
                "ltp": core["ltp"],
                "levels": core["levels"],
                "markers": core["markers"],
                "title": core["title"],
                "height": core["height"],
                "componentKey": core.get("componentKey"),
                "fitContent": False,
            }
        else:
            core["mode"] = "full"

    # Validate before handing data to the bidi transport. Never emit NaN/Inf.
    json.dumps(
        core,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return core


def render(
    df, title, height=900, vwap=True, ltp=None, levels=None, markers=None,
    max_candles=180, fit_content=False
):
    payload = make_payload(
        df, vwap=vwap, ltp=ltp, levels=levels, markers=markers,
        title=title, height=height, max_candles=max_candles,
        fit_content=fit_content, incremental=False
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
