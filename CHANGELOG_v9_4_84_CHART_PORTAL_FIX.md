# v9.4.84 — live chart portal/tab freeze fix

- Fixed the body-level live-chart portal remaining visible when its Streamlit
  tab/panel was hidden. This caused a stale/frozen chart to appear over other
  tabs after switching tabs.
- The chart portal now tracks slot visibility with layout/intersection/mutation
  observers and hides itself whenever its owning tab is inactive.
- The chart portal is explicitly removed when the V2 component is genuinely
  unmounted, preventing old charts from surviving page navigation.
- The existing stable component key and incremental live candle transport are
  unchanged, so active live charts continue updating in place during background
  EOD recovery.
