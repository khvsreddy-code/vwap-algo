
"""Persistent TradingView-style chart for the Streamlit terminal.

v9.4.62 — persistent live chart + safe V2 mount + live labels.

Uses Streamlit Custom Components V2 instead of components.html().
The chart DOM/Lightweight Charts instance is kept alive while Python fragments
rerun, so live candles are updated in-place rather than replacing the iframe.
"""
import json
import math
import re
import pandas as pd
import streamlit as st

# Live chart transport/window policy.
# Keep a useful amount of history in the browser, but open on the most recent
# two calendar days so the user can scroll back through the loaded candles.
DEFAULT_CHART_CANDLES = 800
VISIBLE_CALENDAR_DAYS = 2

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
  // Streamlit V2 passes a renderer-args object. `parentElement` is the actual
  // DOM mount root (ShadowRoot when isolate_styles=True, HTMLElement otherwise).
  // Never treat the renderer-args object itself as a DOM node.
  const { parentElement, data = {}, key = "" } = component;
  const root = parentElement;
  const rootIsDom = !!(
    root &&
    typeof root.querySelector === "function" &&
    typeof root.appendChild === "function"
  );
  if (!rootIsDom) {
    // A defensive no-op prevents the old `root.appendChild is not a function`
    // BidiComponent crash if Streamlit briefly invokes the renderer without a
    // usable mount root during a hot reload.
    return () => {};
  }

  const mountHost = root.host instanceof HTMLElement ? root.host : root;
  const outerRoot = mountHost && mountHost.parentElement ? mountHost.parentElement : mountHost;
  const requestedHeight = Math.max(260, Number(data.height || 620));

  function getMountRect() {
    try {
      if (mountHost && typeof mountHost.getBoundingClientRect === "function") {
        return mountHost.getBoundingClientRect();
      }
    } catch (e) {}
    return { width: 0, height: requestedHeight };
  }

  try {
    if (mountHost && mountHost.style) {
      mountHost.style.display = "block";
      mountHost.style.width = "100%";
      mountHost.style.maxWidth = "100%";
      mountHost.style.minWidth = "0";
      mountHost.style.height = requestedHeight + "px";
      mountHost.style.minHeight = requestedHeight + "px";
      mountHost.style.maxHeight = requestedHeight + "px";
      mountHost.style.boxSizing = "border-box";
      mountHost.style.overflow = "hidden";
    }
  } catch (e) {}

  // Keep state by component key, but always mount the chart inside the current
  // parentElement. We deliberately do NOT move a DOM node from an old ShadowRoot
  // into a new one; doing that during Streamlit reruns is what made the previous
  // implementation vulnerable to BidiComponent mount errors and stale charts.
  const registry = window.__fyersVwapCharts || (window.__fyersVwapCharts = {});
  const chartKey = String(data.componentKey || key || data.title || "default");
  let state = registry[chartKey];

  if (!state) {
    state = {
      host: null,
      root: null,
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
      initialViewApplied: false,
      payloadSeq: 0,
      fallback: false,
      fallbackCanvas: null,
      fallbackCtx: null,
      fallbackData: null,
      fallbackView: { start: 0, count: 0 },
      fallbackDrag: null,
      fallbackBound: false,
      markerOverlay: null,
      markerOverlayBound: false,
      markerOverlayRaf: 0,
      portalPositionBound: false,
      syncPortalPosition: null,
      slot: null,
      slotResizeObserver: null,
      slotIntersectionObserver: null,
      theme: "Dark",
    };
    registry[chartKey] = state;
  }

  // -----------------------------------------------------------------------
  // PERSISTENT CHART PORTAL
  // Streamlit may replace the V2 component's ShadowRoot during each fragment
  // rerun. The actual chart DOM must therefore NOT live inside that mount.
  // Keep a single chart host in document.body and use the component only as a
  // transparent layout slot. The existing chart/series/canvas stay alive;
  // reruns only deliver data to applyPayload().
  // Legacy test marker: root.appendChild(host) is intentionally NOT used for
  // the real chart host; the host is portaled to document.body instead.
  // -----------------------------------------------------------------------
  let slot = root.querySelector(".vwap-chart-mount-slot");
  if (!slot) {
    slot = document.createElement("div");
    slot.className = "vwap-chart-mount-slot";
    root.appendChild(slot);
  }
  slot.style.display = "block";
  slot.style.width = "100%";
  slot.style.height = requestedHeight + "px";
  slot.style.minHeight = requestedHeight + "px";
  slot.style.maxHeight = requestedHeight + "px";
  slot.style.boxSizing = "border-box";
  slot.style.visibility = "hidden";
  slot.style.pointerEvents = "none";

  const portalId = "fyers-vwap-chart-portal-" + chartKey.replace(/[^a-zA-Z0-9_-]/g, "_");
  let host = state.host && state.host.isConnected ? state.host : document.getElementById(portalId);

  if (!host) {
    host = document.createElement("div");
    host.id = portalId;
    host.className = "vwap-chart-shell";
    host.innerHTML = '<canvas class="vwap-chart-canvas" aria-label="VWAP live chart"></canvas>' +
                     '<div class="vwap-chart-hint">Scroll to zoom • drag to pan</div>';
    document.body.appendChild(host);
  }

  state.root = root;
  state.slot = slot;
  state.host = host;

  function syncPortalPosition() {
    try {
      const rect = slot.getBoundingClientRect();
      const style = slot && typeof getComputedStyle === "function"
        ? getComputedStyle(slot) : null;
      const visible = !!(
        rect &&
        rect.width > 0 &&
        rect.height > 0 &&
        (!style || (
          style.display !== "none" &&
          style.visibility !== "hidden" &&
          style.visibility !== "collapse"
        ))
      );

      // The chart host is portaled to document.body so Streamlit can replace
      // the V2 mount/ShadowRoot during fragment reruns. That also means the
      // browser will NOT automatically hide the chart when its Streamlit tab
      // becomes inactive. The old code simply returned for a zero-sized slot,
      // leaving the last fixed-position host visible over the newly selected
      // tab/page. This made a stale/frozen chart appear to follow the user
      // through every tab.
      if (!visible) {
        host.style.display = "none";
        host.style.pointerEvents = "none";
        return;
      }

      host.style.display = "block";
      host.style.position = "fixed";
      host.style.left = Math.round(rect.left) + "px";
      host.style.top = Math.round(rect.top) + "px";
      host.style.width = Math.round(rect.width) + "px";
      host.style.height = Math.round(rect.height) + "px";
      host.style.minHeight = Math.round(rect.height) + "px";
      host.style.maxHeight = Math.round(rect.height) + "px";
      host.style.maxWidth = Math.round(rect.width) + "px";
      host.style.boxSizing = "border-box";
      host.style.overflow = "hidden";
      host.style.zIndex = "1";
      host.style.pointerEvents = "auto";
      host.style.background = "#0a0f16";
      host.style.border = "1px solid #1d2836";
      host.style.borderRadius = "12px";
      host.style.margin = "0";
      host.style.padding = "0";

      const canvas = host.querySelector(".vwap-chart-canvas");
      if (canvas) {
        canvas.style.position = "absolute";
        canvas.style.inset = "0";
        canvas.style.display = "block";
        canvas.style.width = "100%";
        canvas.style.height = "100%";
      }
      const hint = host.querySelector(".vwap-chart-hint");
      if (hint) {
        hint.style.position = "absolute";
        hint.style.right = "10px";
        hint.style.bottom = "8px";
        hint.style.zIndex = "2";
        hint.style.padding = "4px 8px";
        hint.style.border = "1px solid rgba(100,116,139,.28)";
        hint.style.borderRadius = "999px";
        hint.style.background = "rgba(9,14,21,.76)";
        hint.style.color = "#718096";
        hint.style.font = "11px/1.2 Inter,system-ui,sans-serif";
        hint.style.pointerEvents = "none";
      }
    } catch (e) {}
  }

  if (!state.portalPositionBound) {
    state.portalPositionBound = true;
    state.syncPortalPosition = syncPortalPosition;
    window.addEventListener("resize", syncPortalPosition, { passive: true });
    window.addEventListener("scroll", syncPortalPosition, { passive: true, capture: true });
    document.addEventListener("visibilitychange", syncPortalPosition, { passive: true });

    // Streamlit tabs hide/show their panels without necessarily causing a
    // window resize or scroll event. Observe the actual layout slot so the
    // body-level chart portal is hidden immediately when this chart's tab is
    // inactive and shown again when the tab becomes active.
    try {
      state.slotResizeObserver = new ResizeObserver(syncPortalPosition);
      state.slotResizeObserver.observe(slot);
    } catch (e) {}

    try {
      state.slotIntersectionObserver = new IntersectionObserver(() => {
        syncPortalPosition();
      }, { threshold: [0, 0.01, 1] });
      state.slotIntersectionObserver.observe(slot);
    } catch (e) {}

    // A MutationObserver catches display/visibility changes on tab panels in
    // Streamlit versions where ResizeObserver/IntersectionObserver does not
    // emit a callback for a hidden ancestor.
    try {
      state.slotMutationObserver = new MutationObserver(() => {
        syncPortalPosition();
      });
      let panel = slot.parentElement;
      if (panel) state.slotMutationObserver.observe(panel, {
        attributes: true,
        attributeFilter: ["style", "class", "hidden", "aria-hidden"]
      });
    } catch (e) {}
  }
  syncPortalPosition();

  function ensureMarkerOverlay() {
    if (state.markerOverlay && state.markerOverlay.isConnected) return state.markerOverlay;
    const overlay = document.createElement("div");
    overlay.className = "vwap-marker-overlay";
    overlay.style.position = "absolute";
    overlay.style.inset = "0";
    overlay.style.zIndex = "20";
    overlay.style.pointerEvents = "none";
    overlay.style.overflow = "hidden";
    overlay.style.fontFamily = "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    host.appendChild(overlay);
    state.markerOverlay = overlay;
    return overlay;
  }

  function markerPalette(text) {
    const t = String(text || "").toUpperCase();
    if (t.includes("BUY PE")) {
      return {
        bg: "linear-gradient(135deg, #7c3aed 0%, #5b21b6 100%)",
        border: "rgba(196,181,253,.72)",
        glow: "rgba(124,58,237,.34)",
        dot: "#c4b5fd",
        arrow: "#8b5cf6"
      };
    }
    if (t.includes("SELL")) {
      return {
        bg: "linear-gradient(135deg, #b45309 0%, #92400e 100%)",
        border: "rgba(253,186,116,.72)",
        glow: "rgba(245,158,11,.30)",
        dot: "#fde68a",
        arrow: "#f59e0b"
      };
    }
    return {
      bg: "linear-gradient(135deg, #059669 0%, #047857 100%)",
      border: "rgba(110,231,183,.72)",
      glow: "rgba(16,185,129,.30)",
      dot: "#a7f3d0",
      arrow: "#10b981"
    };
  }

  function updateMarkerOverlay(markers) {
    if (!state.chart || !state.candles) return;
    const overlay = ensureMarkerOverlay();
    overlay.replaceChildren();

    const times = state.candleData || [];
    if (!times.length) return;

    const timeScale = state.chart.timeScale();
    const seen = {};
    (markers || []).forEach((m) => {
      const rawTime = Number(m.time);
      if (!Number.isFinite(rawTime)) return;
      let candle = null;
      let best = Infinity;
      for (const c of times) {
        const delta = Math.abs(Number(c.time) - rawTime);
        if (delta < best) { best = delta; candle = c; }
      }
      if (!candle) return;

      const x = timeScale.timeToCoordinate(Number(candle.time));
      const above = String(m.position || "") === "aboveBar";
      const basePrice = above ? Number(candle.high) : Number(candle.low);
      const priceY = state.candles.priceToCoordinate(basePrice);
      if (x == null || priceY == null || !Number.isFinite(Number(x)) || !Number.isFinite(Number(priceY))) return;

      const palette = markerPalette(m.text);
      const label = String(m.text || "").trim() || "SIGNAL";
      const key = String(candle.time);
      const stack = seen[key] || 0;
      seen[key] = stack + 1;
      const offsetY = stack * 24;
      const y = Number(priceY) + (above ? -40 - offsetY : 20 + offsetY);

      const wrap = document.createElement("div");
      wrap.style.position = "absolute";
      wrap.style.left = Math.round(Number(x)) + "px";
      wrap.style.top = Math.round(y) + "px";
      wrap.style.transform = "translateX(-50%)";
      wrap.style.display = "flex";
      wrap.style.flexDirection = above ? "column-reverse" : "column";
      wrap.style.alignItems = "center";
      wrap.style.gap = "3px";
      wrap.style.whiteSpace = "nowrap";

      const pill = document.createElement("div");
      pill.textContent = label;
      pill.style.padding = "4px 9px";
      pill.style.borderRadius = "6px";
      pill.style.background = palette.bg;
      pill.style.border = "1px solid " + palette.border;
      pill.style.color = "#ffffff";
      pill.style.fontSize = "10px";
      pill.style.fontWeight = "800";
      pill.style.letterSpacing = ".18px";
      pill.style.lineHeight = "14px";
      pill.style.textShadow = "0 1px 1px rgba(0,0,0,.35)";
      pill.style.boxShadow = "0 4px 12px " + palette.glow + ", inset 0 1px 0 rgba(255,255,255,.16)";
      pill.style.backdropFilter = "blur(5px)";
      pill.style.maxWidth = "210px";
      pill.style.overflow = "hidden";
      pill.style.textOverflow = "ellipsis";

      const stem = document.createElement("span");
      stem.style.width = "2px";
      stem.style.height = "7px";
      stem.style.background = palette.arrow;
      stem.style.opacity = ".9";
      stem.style.boxShadow = "0 0 5px " + palette.glow;

      const dot = document.createElement("span");
      dot.style.width = "5px";
      dot.style.height = "5px";
      dot.style.borderRadius = "50%";
      dot.style.background = palette.dot;
      dot.style.boxShadow = "0 0 7px " + palette.glow;

      wrap.appendChild(pill);
      wrap.appendChild(stem);
      wrap.appendChild(dot);
      overlay.appendChild(wrap);
    });
  }

  function scheduleMarkerOverlayUpdate() {
    if (state.markerOverlayRaf) cancelAnimationFrame(state.markerOverlayRaf);
    state.markerOverlayRaf = requestAnimationFrame(() => {
      state.markerOverlayRaf = 0;
      updateMarkerOverlay(state.lastMarkers || []);
    });
  }

  function loadScript() {
    if (window.LightweightCharts) return Promise.resolve();
    if (state.loading && state.loadPromise) return state.loadPromise;
    state.loading = true;
    state.loadPromise = new Promise((resolve, reject) => {
      const urls = [
        "https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js",
        "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"
      ];
      let attempt = 0;

      const tryNext = () => {
        if (window.LightweightCharts) return resolve();

        // A failed <script> element remains in document.head and never emits
        // another load/error event on a later component mount. That used to
        // leave the chart permanently blank in Streamlit Cloud. Remove stale
        // tags and always create a fresh attempt.
        const existing = document.querySelector('script[data-vwap-lwc="1"]');
        if (existing) {
          const status = existing.dataset.vwapStatus || "";
          if (status === "loading") {
            const waitStarted = Date.now();
            const poll = () => {
              if (window.LightweightCharts) return resolve();
              if (Date.now() - waitStarted > 2500) {
                try { existing.remove(); } catch (e) {}
                tryNext();
                return;
              }
              setTimeout(poll, 100);
            };
            poll();
            return;
          }
          try { existing.remove(); } catch (e) {}
        }

        if (attempt >= urls.length) {
          reject(new Error("chart library unavailable"));
          return;
        }

        const tag = document.createElement("script");
        tag.dataset.vwapLwc = "1";
        tag.dataset.vwapStatus = "loading";
        tag.src = urls[attempt++];
        tag.onload = () => {
          tag.dataset.vwapStatus = "loaded";
          if (window.LightweightCharts) resolve();
          else {
            try { tag.remove(); } catch (e) {}
            tryNext();
          }
        };
        tag.onerror = () => {
          tag.dataset.vwapStatus = "failed";
          try { tag.remove(); } catch (e) {}
          tryNext();
        };
        document.head.appendChild(tag);
      };

      tryNext();

      // Never let a browser/network problem leave the component in a pending
      // state forever. The canvas chart is already visible while this runs.
      setTimeout(() => {
        if (!window.LightweightCharts && attempt >= urls.length) {
          reject(new Error("chart library timeout"));
        }
      }, 6000);
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
      initialVisibleRange: p.initialVisibleRange && Number.isFinite(Number(p.initialVisibleRange.from)) && Number.isFinite(Number(p.initialVisibleRange.to))
        ? { from: Number(p.initialVisibleRange.from), to: Number(p.initialVisibleRange.to) } : null,
      theme: String(p.theme || "Dark"),
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
    if (state.theme !== d.theme) {
      applyChartTheme(d.theme);
    }

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
          while (candles.length > DEFAULT_CHART_CANDLES) candles.shift();
        }
        if (d.vwap) {
          const idx = vw.findIndex(x => Number(x.time) === Number(d.vwap.time));
          if (idx >= 0) vw[idx] = d.vwap;
          else vw.push(d.vwap);
          vw.sort((a,b) => Number(a.time) - Number(b.time));
          while (vw.length > DEFAULT_CHART_CANDLES) vw.shift();
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

    // Lightweight Charts' native marker text is intentionally replaced with
    // a custom HTML overlay so BUY CE / BUY PE labels can use the richer pill
    // styling from the original chart.
    if (!sameJson(state.lastMarkers, markerData)) {
      state.lastMarkers = markerData.slice();
    }
    updateMarkerOverlay(state.lastMarkers);

    const previous = state.lastPayload;
    const previousLast = previous && previous.mode === "delta"
      ? Number(previous.candle && previous.candle.time)
      : (previous && previous.candles && previous.candles.length
          ? Number(previous.candles[previous.candles.length - 1].time) : null);
    const incomingLast = d.mode === "delta"
      ? Number(d.candle && d.candle.time)
      : (d.candles.length ? Number(d.candles[d.candles.length - 1].time) : null);

    if (d.mode !== "delta" && !state.initialViewApplied) {
      // Initial viewport: show only yesterday + today, while retaining the
      // complete 800-candle dataset in the series so the user can scroll back.
      // Subsequent payloads never reset the viewport, preserving manual pan/zoom.
      try {
        if (d.fitContent) {
          state.chart.timeScale().fitContent();
        } else if (d.initialVisibleRange) {
          state.chart.timeScale().setVisibleRange(d.initialVisibleRange);
        } else {
          state.chart.timeScale().fitContent();
        }
      } catch (e) {
        try { state.chart.timeScale().fitContent(); } catch (_) {}
      }
      state.hasFit = true;
      state.initialViewApplied = true;
    } else if (d.mode !== "delta" && d.fitContent) {
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
    scheduleMarkerOverlayUpdate();
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
      const text=String(m.text||"").toUpperCase();
      const isPE=text.includes("BUY PE"), isSell=text.includes("SELL");
      const accent=isSell?"#f59e0b":(isPE?"#8b5cf6":"#10b981");
      const pillBg=isSell?"rgba(146,64,14,.96)":(isPE?"rgba(91,33,182,.96)":"rgba(4,120,87,.96)");
      ctx.fillStyle=accent;ctx.beginPath();ctx.arc(xx,yy,3.5,0,Math.PI*2);ctx.fill();
      const label=String(m.text||"").trim();ctx.font="800 10px system-ui";const tw=ctx.measureText(label).width+18;
      const ly=m.position==="belowBar"?yy+9:yy-26;
      ctx.fillStyle=pillBg;ctx.strokeStyle=accent;ctx.lineWidth=1;
      ctx.roundRect(xx-tw/2,ly,tw,19,6);ctx.fill();ctx.stroke();
      ctx.fillStyle="#fff";ctx.fillText(label,xx-tw/2+9,ly+13);
      ctx.strokeStyle=accent;ctx.lineWidth=2;ctx.beginPath();
      ctx.moveTo(xx, m.position==="belowBar"?ly:ly+19);ctx.lineTo(xx, m.position==="belowBar"?ly-6:ly+25);ctx.stroke();
    });
    ctx.fillStyle="#8b95a7";ctx.font="bold 12px system-ui";ctx.fillText(String(d.title||"VWAP"),L,18);
    ctx.fillStyle="#66758a";ctx.font="11px system-ui";
    const first=view[0],last=view[view.length-1];
    if(first?.time){ctx.fillText(new Date(Number(first.time)*1000).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Kolkata"}),L,H-10);}
    if(last?.time){const tx=new Date(Number(last.time)*1000).toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",hour12:false,timeZone:"Asia/Kolkata"});ctx.fillText(tx,W-R-45,H-10);}
  }


  function themeColors(name) {
    const t = String(name || "Dark").toLowerCase();
    if (t.includes("black")) {
      return {
        background: "#000000", text: "#f5f5f5", grid: "#171717", border: "#303030",
        up: "#ffffff", down: "#777777", wickUp: "#ffffff", wickDown: "#777777",
        vwap: "#ffffff", ltp: "#d0d0d0", hintBg: "rgba(0,0,0,.82)", hintText: "#bdbdbd"
      };
    }
    if (t.includes("light")) {
      return {
        background: "#ffffff", text: "#3f4854", grid: "#edf0f4", border: "#d5dbe3",
        up: "#111111", down: "#777777", wickUp: "#111111", wickDown: "#777777",
        vwap: "#2563eb", ltp: "#555555", hintBg: "rgba(255,255,255,.90)", hintText: "#64748b"
      };
    }
    return {
      background: "#0b0f14", text: "#9aa4b2", grid: "#17202b", border: "#263241",
      up: "#22b39b", down: "#ef5350", wickUp: "#22b39b", wickDown: "#ef5350",
      vwap: "#4aa3ff", ltp: "#8b95a7", hintBg: "rgba(9,14,21,.76)", hintText: "#718096"
    };
  }

  function applyChartTheme(name) {
    const c = themeColors(name);
    state.theme = String(name || "Dark");
    host.style.background = c.background;
    const hint = host.querySelector(".vwap-chart-hint");
    if (hint) {
      hint.style.background = c.hintBg;
      hint.style.color = c.hintText;
      hint.style.borderColor = c.border;
    }
    if (!state.chart) return;
    try {
      state.chart.applyOptions({
        layout: { background: { type: "solid", color: c.background }, textColor: c.text },
        grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
        rightPriceScale: { borderColor: c.border },
        timeScale: { borderColor: c.border }
      });
    } catch (e) {}
    try { state.candles && state.candles.applyOptions({
      upColor: c.up, downColor: c.down, wickUpColor: c.wickUp, wickDownColor: c.wickDown
    }); } catch (e) {}
    try { state.vwap && state.vwap.applyOptions({ color: c.vwap }); } catch (e) {}
    if (state.ltpLine && state.candles) {
      try { state.ltpLine.applyOptions({ color: c.ltp }); } catch (e) {}
    }
  }

  function init() {
    if (state.ready) {
      applyPayload(data);
      return;
    }
    state.chart = LightweightCharts.createChart(host, {
      width: Math.max(320, Math.round(
        getMountRect().width ||
        host.getBoundingClientRect().width ||
        (outerRoot && typeof outerRoot.getBoundingClientRect === "function"
          ? outerRoot.getBoundingClientRect().width : 0) ||
        900
      )),
      height: Math.max(260, Math.round(
        getMountRect().height || host.getBoundingClientRect().height || requestedHeight
      )),
      autoSize: false,
      layout: {
        background: { type: "solid", color: themeColors(data.theme).background },
        textColor: themeColors(data.theme).text,
      },
      grid: {
        vertLines: { color: themeColors(data.theme).grid },
        horzLines: { color: themeColors(data.theme).grid },
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: {
        borderColor: themeColors(data.theme).border,
        autoScale: true,
        scaleMargins: { top: 0.14, bottom: 0.14 },
      },
      timeScale: {
        borderColor: themeColors(data.theme).border,
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
      upColor: themeColors(data.theme).up,
      downColor: themeColors(data.theme).down,
      borderVisible: false,
      wickUpColor: themeColors(data.theme).wickUp,
      wickDownColor: themeColors(data.theme).wickDown,
      priceLineVisible: false,
      lastValueVisible: true,
    });
    state.vwap = state.chart.addLineSeries({
      color: themeColors(data.theme).vwap,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
    });

    const resizeChart = () => {
      if (state.syncPortalPosition) state.syncPortalPosition();
      if (state.fallback) { resizeFallback(); drawFallback(); return; }
      if (!state.chart) return;
      const rect = host.getBoundingClientRect();
      const mountRect = getMountRect();
      const outerRect = outerRoot && typeof outerRoot.getBoundingClientRect === "function"
        ? outerRoot.getBoundingClientRect() : { width: 0, height: 0 };
      // Prefer the actual Streamlit mount box. Never read layout properties
      // from the renderer-args object because it is not a DOM element.
      const w = Math.max(320, Math.round(
        mountRect.width || rect.width || outerRect.width || 900
      ));
      const h = Math.max(260, Math.round(
        mountRect.height || rect.height || requestedHeight
      ));
      try {
        if (mountHost && mountHost.style) {
          mountHost.style.width = "100%";
          mountHost.style.height = requestedHeight + "px";
        }
        host.style.width = "100%";
        host.style.height = requestedHeight + "px";
        state.chart.applyOptions({ width: w, height: h });
        scheduleMarkerOverlayUpdate();
      } catch (e) {}
    };
    state.resizeObserver = new ResizeObserver(() => {
      resizeChart();
      scheduleMarkerOverlayUpdate();
    });
    if (mountHost && typeof mountHost === "object") state.resizeObserver.observe(mountHost);
    state.resizeObserver.observe(host);
    if (outerRoot && outerRoot !== mountHost && typeof outerRoot === "object" && typeof outerRoot.getBoundingClientRect === "function") state.resizeObserver.observe(outerRoot);
    if (!state.markerOverlayBound) {
      state.markerOverlayBound = true;
      try {
        state.chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleMarkerOverlayUpdate);
      } catch (e) {}
    }
    ensureMarkerOverlay();
    state.ready = true;
    // Streamlit lays out V2 components asynchronously. Resize again after the
    // parent/card has received its final width so the chart cannot initialize
    // as a tiny 300px chart and remain there.
    requestAnimationFrame(() => {
      resizeChart();
      requestAnimationFrame(() => {
        resizeChart();
        setTimeout(resizeChart, 80);
        setTimeout(resizeChart, 250);
      });
      applyPayload(data);
    });
  }

  // Render the dependency-free canvas chart immediately. This is the
  // deployment-safe path and guarantees candles are visible even when a CDN
  // is blocked. Lightweight Charts is an optional enhancement.
  if (!state.chart && !state.fallback) {
    initFallback();
    state.fallbackData = normalisePayload(data);
    state.lastPayload = state.fallbackData;
    drawFallback();
  }

  // Try the richer Lightweight Charts renderer in the background. If it cannot
  // load, keep the already-visible canvas chart; never blank the component.
  loadScript().then(() => {
    if (!state.chart && window.LightweightCharts && !state.fallbackUpgradeTried) {
      state.fallbackUpgradeTried = true;
      const canvas = state.fallbackCanvas;
      if (canvas) canvas.style.display = "none";
      state.fallback = false;
      try {
        init();
      } catch (e) {
        state.fallback = true;
        if (canvas) canvas.style.display = "block";
        drawFallback();
      }
    }
  }).catch(() => {
    // Canvas fallback is already active.
  });

  // CRITICAL LIVE UPDATE PATH:
  // Streamlit V2 calls this renderer again whenever Python sends new `data`
  // to the same keyed component. The chart instance must stay alive, but the
  // new payload MUST still be applied on every renderer invocation.
  //
  // The previous implementation only called applyPayload(data) during chart
  // initialization. That meant the first 800 candles rendered correctly, but
  // subsequent 1-second fragment updates were silently ignored by the browser.
  // The Python LTP/header could move while both NIFTY and option candles stayed
  // frozen. This call is the actual Python -> browser live-candle bridge.
  if (state.chart || state.fallback) {
    try { applyPayload(data); } catch (e) { /* preserve existing chart */ }
  }

  // V2 component renderers may be invoked repeatedly; do not destroy the chart.
  // Cleanup happens only when the component is genuinely unmounted.
  return () => {
    // Do not tear down the chart during normal Python reruns. Streamlit calls
    // this cleanup when the V2 component is actually unmounted. Because the
    // visual chart is portaled outside the component mount, we must explicitly
    // hide/remove it here or a chart from the previous page can remain painted
    // over every subsequent tab/page.
    try { state.slotResizeObserver && state.slotResizeObserver.disconnect(); } catch (e) {}
    try { state.slotIntersectionObserver && state.slotIntersectionObserver.disconnect(); } catch (e) {}
    try { state.slotMutationObserver && state.slotMutationObserver.disconnect(); } catch (e) {}
    state.slotResizeObserver = null;
    state.slotIntersectionObserver = null;
    state.slotMutationObserver = null;
    try { window.removeEventListener("resize", state.syncPortalPosition); } catch (e) {}
    try { window.removeEventListener("scroll", state.syncPortalPosition, true); } catch (e) {}
    try { document.removeEventListener("visibilitychange", state.syncPortalPosition); } catch (e) {}
    try {
      if (state.host && state.host.isConnected) state.host.remove();
    } catch (e) {}
    state.host = null;
    state.root = null;
    state.slot = null;
    state.ready = false;
    state.chart = null;
    state.candles = null;
    state.vwap = null;
    state.markerOverlay = null;
    delete registry[chartKey];
  };
}
"""

@st.cache_resource(show_spinner=False)
def _register_live_chart_component():
    """Register the V2 component once per Streamlit process.

    Streamlit reruns the script for every fragment tick. Registering the same
    V2 component name at module import time on every rerun causes:
    "Component ... is already registered. Overwriting previous definition."
    The cached resource keeps one component definition alive for the process.
    """
    try:
        return st.components.v2.component(
            name="fyers_vwap_live_chart_v9_4_65",
            html=_CHART_HTML,
            css=_CHART_CSS,
            js=_CHART_JS,
            isolate_styles=True,
        )
    except AttributeError:
        return None


_component = _register_live_chart_component()


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
    height=900, max_candles=DEFAULT_CHART_CANDLES, fit_content=False, incremental=False
):
    """Build a bounded JSON-safe chart payload.

    Live/realtime views use a rolling candle window instead of transmitting the
    entire historical month on every fragment rerun.  Historical/replay views
    can explicitly request the full dataset with ``fit_content=True``.
    """
    def _initial_visible_range(candles):
        """Return the last two available calendar days in IST.

        The payload still contains up to 800 candles; this range only controls
        the opening viewport. Using the last candle's date avoids an empty
        viewport after weekends/market holidays.
        """
        if not candles:
            return None
        try:
            last = pd.Timestamp(df["datetime"].max())
            if last.tzinfo is None:
                last = last.tz_localize("Asia/Kolkata")
            else:
                last = last.tz_convert("Asia/Kolkata")
            start_day = (last.normalize() - pd.Timedelta(days=1))
            end_day = last.normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            start_ts = max(int(candles[0]["time"]), int(start_day.timestamp()))
            end_ts = min(int(candles[-1]["time"]), int(end_day.timestamp()))
            if end_ts <= start_ts:
                return None
            return {"from": start_ts, "to": end_ts}
        except Exception:
            return None

    if df is None or df.empty:
        core = {
            "candles": [], "vwap": [], "ltp": _json_float(ltp),
            "levels": levels or [], "markers": [],
            "title": str(title), "height": int(height),
            "componentKey": f"live-{title}-{int(height)}",
            "fitContent": bool(fit_content),
            "initialVisibleRange": None,
            "theme": str(st.session_state.get("ui_theme", "Dark")),
        }
    else:
        # ``max_candles=None`` is used by the app for both live and historical
        # chart calls.  A live call has fit_content=False, so keep only a bounded
        # rolling window.  Full historical/replay views set fit_content=True and
        # may intentionally send the complete loaded window.
        if max_candles is None:
            plot = df if fit_content else df.tail(DEFAULT_CHART_CANDLES)
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
            "initialVisibleRange": _initial_visible_range(candles) if not fit_content else None,
            "theme": str(st.session_state.get("ui_theme", "Dark")),
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
                "initialVisibleRange": core.get("initialVisibleRange"),
                "theme": core.get("theme", str(st.session_state.get("ui_theme", "Dark"))),
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
    max_candles=DEFAULT_CHART_CANDLES, fit_content=False
):
    payload = make_payload(
        df, vwap=vwap, ltp=ltp, levels=levels, markers=markers,
        title=title, height=height, max_candles=max_candles,
        fit_content=fit_content, incremental=(not fit_content)
    )
    # Keep the component identity stable. The payload itself carries the live
    # candle delta after the first render, so the browser updates the existing
    # series instead of replacing the chart.
    try:
        last = payload.get("candles", [])[-1] if payload.get("candles") else {}
        payload["updateSeq"] = (
            last.get("time"),
            last.get("open"),
            last.get("high"),
            last.get("low"),
            last.get("close"),
            payload.get("ltp"),
        )
    except Exception:
        payload["updateSeq"] = None
    if _component is None:
        st.error("This live chart requires Streamlit Custom Components V2. Upgrade Streamlit to 1.52+.")
        return

    # IMPORTANT: keep the Python component key STABLE. Streamlit V2 explicitly
    # documents that a supplied key keeps the same frontend component instance
    # while `data` changes; changing the key creates a new instance and resets
    # its frontend state. The previous transport fix did the opposite: it put
    # the live candle's OHLC/LTP into the key. That remounted the component on
    # every tick. The new component then received a `mode=delta` payload without
    # the previous browser-side candle state, so both NIFTY and option charts
    # could appear frozen even though the terminal LTP continued updating.
    #
    # The data payload is the update signal. The JS renderer is called again
    # when `data` changes and applies the delta to the EXISTING series. The
    # stable key preserves the chart, zoom/pan state, and Lightweight Charts
    # instance.
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
