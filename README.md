# FYERS VWAP Trader V9.4.54 — Streamlit deployment + UI reliability

## Run

```bash
streamlit run app.py
```

## V9.4.50 — VWAP setup-quality + bounded live chart

This version keeps the existing 15-point confirmation rule and 8-candle confirmation window, while expanding the ways a valid VWAP setup can be armed.


### V9.4.54 — Streamlit deployment reliability + UI polish

- Added a dependency-free canvas chart fallback. If Lightweight Charts cannot be fetched by the browser because Streamlit Cloud, CSP, an ad blocker, proxy, or an offline environment blocks the CDN, the terminal still renders candles, VWAP, LTP, levels and entry labels.
- Chart wrappers now own their height explicitly and resize from the actual Streamlit component bounds.
- BUY CE / BUY PE labels remain visible on the fallback renderer and on Lightweight Charts.
- Removed the `order_ws` NameError path by importing `order_ws` explicitly from `fyers_apiv3.FyersWebsocket` and treating the order socket as optional.
- Order WebSocket failures no longer stop or visually break the market-data/chart feed.
- Order WebSocket subscription follows the current FYERS v3 sample (`OnOrders,OnTrades,OnPositions,OnGeneral`).
- Refreshed the terminal visual system with glassy/dark cards, stronger hierarchy, better metrics, improved navigation, status surfaces and table framing.
- Chart payloads are sent as bounded full state on rerender to avoid stale delta state after Streamlit component remounts.

### VWAP entries

The strategy now recognizes:

- **CLOSE_CROSS** — a completed candle genuinely changes sides across VWAP.
- **VWAP_RECLAIM** — price was on the opposite side, tests/reclaims VWAP, and closes back through it.
- **VWAP_BOUNCE** — price is already on one side, pulls through VWAP, and closes back on that side.
- **VWAP_INTERACTION** — small-body/rounded VWAP reclaim or rejection candles are retained as a distinct setup type.
- **FAILED_CROSS** — when an armed cross fails on the very next completed candle, the stale direction can be replaced by the opposite VWAP setup.

A wick that merely touches VWAP is not enough for the bounce path. The candle must make a meaningful VWAP interaction. The original 15-point confirmation is never bypassed.

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

The V9.4.50 package includes regression coverage for:

- strict VWAP confirmation
- rounded VWAP interaction
- VWAP bounce/retest
- failed-cross reversal
- wick-only VWAP touch rejection
- bounded/incremental live-chart transport
- visible-window marker filtering

The development test suite passes before packaging.


### V9.4.58 — long-option-only entry logic

The VWAP directional signal is now mapped to long option entries:
- bullish signal → **BUY CE**
- bearish signal → **BUY PE**
- no bearish `SELL` entry is sent to FYERS
- the underlying directional signal is retained internally as `signal_side` for strategy calculations
- option protection is long-option based (SL below premium, target above premium)
- closing an already-open long option remains a SELL/close action where required by the broker; this is not a new strategy entry.


### V9.4.58 — candle rendering reliability

- Fixed the blank terminal chart caused by empty/stale historical payloads.
- FYERS history now retries intraday requests using an exact epoch range when the calendar-date request returns no candles.
- Historical OHLC data is sorted and de-duplicated before it reaches the chart, satisfying Lightweight Charts' strict timestamp requirements.
- The dependency-free canvas fallback now continues receiving full and delta payloads after a CDN/library failure.
- Existing long-option-only BUY CE / BUY PE behavior is unchanged.


### V9.4.58 — deterministic Streamlit candle bootstrap

The terminal now synchronously seeds FYERS history before the first chart render when the background engine has not populated its dataframe yet. Index history requests no longer send the continuous-futures flag, and the chart retains a dependency-free canvas recovery path.
