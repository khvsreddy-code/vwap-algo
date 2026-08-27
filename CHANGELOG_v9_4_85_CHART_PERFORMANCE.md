# v9.4.85 — live chart performance/smoothness

- The terminal dashboard remains on its 2-second UI cadence, while the Chart tab
  runs as a dedicated nested 1-second Streamlit fragment. This makes the live
  chart update faster without forcing the portfolio/strategy/order panels to
  redraw every second.
- The live chart transport now has a steady-state Python fast path: after the
  initial 800-bar window (and on candle rollover), intrabar ticks send only the
  current candle/VWAP/LTP delta instead of rebuilding and sorting the entire
  chart dataframe.
- Browser portal positioning is coalesced with `requestAnimationFrame` so
  resize/scroll/tab observer bursts do not repeatedly force layout work.
- Marker snapping uses binary search rather than scanning the full candle window.
- Marker overlays are rebuilt only when marker payloads actually change.
- Added CSS containment to reduce chart paint/layout interference with the rest
  of the Streamlit page.
- Existing v9.4.84 tab-visibility/unmount behavior and stable component identity
  are preserved.


## Hotfix — v9.4.85 smooth-chart regression

- Fixed the blank-chart regression caused by the portal layout slot being marked
  `visibility:hidden` while the portal visibility check used that same property
  to decide whether the chart should be displayed.
- The layout slot is now transparent (`opacity:0`) so it preserves geometry
  without hiding the portaled chart.
- Lightweight Charts no longer hides the working canvas fallback before its
  own initialization succeeds; a CDN/runtime failure therefore cannot leave a
  blank chart.
- Live terminal chart history no longer performs a full dataframe concat/sort
  on every 1-second fragment tick. The cached REST window is reused and only the
  current engine candle is patched.
- Historical BUY CE / BUY PE marker calculation is cached by chart/strategy
  configuration instead of replaying the entire historical state machine every
  second.
- Matching engine-timeframe charts reuse the engine's existing VWAP column
  instead of recomputing the full ATR/ADX/VWAP preparation pipeline every tick.
- Existing stable component identity, incremental candle transport, tab
  visibility behavior, and chart labels are preserved.
