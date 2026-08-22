# FYERS VWAP Trader V9.4.53

## Run

```bash
streamlit run app.py
```

## V9.4.53 — VWAP setup-quality + bounded live chart

This version keeps the existing 15-point confirmation rule and 8-candle confirmation window, while expanding the ways a valid VWAP setup can be armed.

### VWAP entries

The strategy now recognizes:

- **CLOSE_CROSS** — a completed candle genuinely changes sides across VWAP.
- **VWAP_RECLAIM** — price was on the opposite side, tests/reclaims VWAP, and closes back through it.
- **VWAP_BOUNCE** — price is already on one side, pulls through VWAP, and closes back on that side.
- **VWAP_INTERACTION** — small-body/rounded VWAP reclaim or rejection candles are retained as a distinct setup type.
- **FAILED_CROSS** — when an armed cross fails on the very next completed candle, the stale direction can be replaced by the opposite VWAP setup.

A wick that merely touches VWAP is not enough for the bounce path. The candle must make a meaningful VWAP interaction. The original 15-point confirmation is never bypassed.

### V9.4.53 regime-aware improvements

The strategy now distinguishes **clear trend alignment** from **range/chop** without imposing a daily trade quota.

- Strong trends (ADX >= 25 by default) reject counter-trend VWAP signals when VWAP slope and price confirm the opposite direction.
- A range no longer acts as a blanket no-trade state: ordinary close-crosses remain blocked, but higher-quality VWAP reclaim/bounce/rejection structures can still arm.
- Added **VWAP_REJECTION** for rounded candles that test VWAP with a meaningful wick and close back on the trend side.
- The existing failed-cross path remains available for false-breakout reversals.
- The original 15-point confirmation and 8-candle window are unchanged.

This is designed to improve trade quality while preserving multiple independent VWAP cycles on normal days; it does **not** force a minimum or maximum number of entries.

### Chop protection

Weak repeated VWAP oscillations are filtered when the market repeatedly flips around VWAP in a short window. A sufficiently strong displacement can override the chop filter.

This is intended to reduce low-quality BUY CE / BUY PE alternation without blocking a decisive move.

### Replay/backtest consistency

The same `VwapConfirmationEngine` setup logic is used by the historical signal/entry paths, so VWAP reclaim, bounce, interaction, and failed-cross entries can appear in replay/backtest markers as well.

Entry markers include the setup type, for example:

- `BUY CE • VWAP_RECLAIM`
- `BUY PE • VWAP_BOUNCE`
- `BUY CE • VWAP_INTERACTION`

### Terminal chart performance

The terminal no longer sends the complete month of candles to the browser on every update.

- Live chart is bounded to the **latest 180 candles**.
- Historical data remains cached in Python.
- Normal ticks update only the current candle/VWAP point.
- When a new timeframe candle rolls in, the bounded 180-candle window is synchronized once.
- Historical markers outside the visible window are not transmitted.
- The existing browser-side Lightweight Charts instance remains mounted; live updates do not call `fitContent()` on every tick.

This is deliberately different from simply slowing down Streamlit reruns.

### Execution path

Market-data processing remains independent of the chart render path. The strategy can confirm an entry from live ticks without waiting for the Streamlit chart to refresh.

The local execution ledger remains the authoritative UI-side record for trigger/entry events, so a chart repaint cannot erase an already-recorded signal.

### Tests

The V9.4.53 package includes regression coverage for:

- strict VWAP confirmation
- rounded VWAP interaction
- VWAP bounce/retest
- failed-cross reversal
- wick-only VWAP touch rejection
- bounded/incremental live-chart transport
- visible-window marker filtering

The development test suite passes before packaging.
