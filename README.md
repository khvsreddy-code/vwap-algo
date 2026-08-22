# FYERS VWAP Trader V10

## Run

```bash
streamlit run app.py
```

## V10 — adaptive VWAP quality engine + persistent incremental live chart

V10 uses the **v9.4.51 regime-aware multi-cycle strategy as the strategy baseline** and adds a setup-quality layer without imposing a daily trade quota.

### Core confirmation rules

- **15-point confirmation** remains unchanged.
- **8-candle confirmation window** remains unchanged.
- Historical replay checks candle high/low so an intrabar confirmation is not missed.
- Live confirmation can occur from ticks without waiting for the UI/chart refresh.

### VWAP setup families

V10 retains:

- `CLOSE_CROSS`
- `VWAP_RECLAIM`
- `VWAP_BOUNCE`
- `VWAP_INTERACTION` for rounded/doji VWAP interactions
- `FAILED_CROSS` for a failed cross that reverses through VWAP

A setup candle never confirms itself.

### New adaptive quality layer

Each candidate setup is scored using information available at that candle/tick:

- VWAP slope alignment
- ADX / directional strength
- normalized distance from VWAP versus ATR
- candle body and close-location quality
- same-side persistence
- recent VWAP flip/chop behavior
- ATR expansion when available
- setup-specific bonuses for reclaim/failed-cross structures

Different setup families have different minimum quality requirements. Warm-up data does not create an artificial no-trade period.

This is deliberately a **quality filter**, not a hard two-trades-per-day limiter. A normal session can still produce multiple independent CE/PE VWAP cycles.

### Replay/live consistency

The same `VwapConfirmationEngine` is used by historical replay/backtest and live processing. Signal metadata now includes the setup type and quality score where available.

### Live chart

`v10_live_chart.py` keeps the browser-side Lightweight Charts instance mounted while Streamlit fragments update.

- ordinary live ticks use incremental `update()`
- a new candle appends/updates only the affected candle
- the existing LTP price line is mutated instead of removed/recreated
- markers are retained and snapped to actual candle timestamps
- historical chart data is not rebuilt on every tick
- chart sizing remains responsive
- no per-tick `fitContent()`

### Important development note

V10 should be compared against the **same historical dataset and configuration that produced the v9.4.51 baseline**. The supplied trade-export CSVs contain executions/results but not the original candle stream, so they cannot by themselves reproduce a strategy backtest.

Before real-money deployment, validate V10 in replay/paper mode and compare:

- win rate
- net P&L
- profit factor
- loss count
- average P&L/trade
- trades/day
- worst day
- consecutive losses

Do not optimize for win rate alone.
