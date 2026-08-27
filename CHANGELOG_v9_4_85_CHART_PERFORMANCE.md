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
