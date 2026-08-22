# FYERS VWAP Trader V10.2

## Run

```bash
streamlit run app.py
```

## V10.2 — 9.4.51 baseline + targeted quality + precision regime layer

V10.2 is built forward from the V9.4.51 regime-aware multi-cycle strategy.
The 9.4.51 behavior remains the reference baseline: the strategy is not given
a daily trade quota and can still take multiple independent VWAP cycles.

The objective is not simply to trade less. It is to remove clearly weak setups
while preserving profitable VWAP behavior.

### Entry behavior retained

- 15-point confirmation.
- 8-candle confirmation window.
- CLOSE_CROSS.
- VWAP_RECLAIM.
- VWAP_BOUNCE.
- VWAP_INTERACTION for rounded/doji-style VWAP interaction.
- FAILED_CROSS reversal.
- Multiple independent CE/PE cycles.
- Live tick confirmation independent of Streamlit rendering.

### New targeted quality layer

Each new VWAP setup is scored using only information available at the setup
candle:

- price location relative to VWAP
- VWAP slope alignment
- ADX/trend strength when warmed up
- normalized displacement from VWAP using ATR
- candle body quality
- close location inside the candle
- recent VWAP flip/chop density

Different setup families use different minimum quality requirements. Reversal
families are intentionally more tolerant because their edge comes from a failed
move/reclaim rather than trend continuation.

A strong established counter-trend is rejected for ordinary CLOSE_CROSS and
VWAP_BOUNCE setups, while FAILED_CROSS/VWAP_RECLAIM/VWAP_INTERACTION remain
available to catch genuine reversals.

Missing indicator warm-up data never silently disables the strategy.

### No artificial trade quota

V10.2 does not enforce two trades per day, a maximum daily trade count, or a
fixed number of entries. If price produces multiple independent valid VWAP
cycles, the strategy can take them.

### Replay/live consistency

The same strategy state machine is used for historical replay and live
processing. Historical confirmation uses candle high/low; live confirmation
uses ticks. Entry metadata includes quality score and regime where available.

### Persistent live chart

The browser-side Lightweight Charts instance remains mounted.

- Historical candles are loaded once for the visible window.
- Current candles use incremental updates.
- New candles are appended instead of rebuilding the entire chart.
- Markers are synchronized without recreating the chart.
- The chart is not part of the trading decision path.

### Validation rule

V10.2 is not considered an improvement merely because it has more filters.
V9.4.51 remains the benchmark. The same historical candle dataset and
backtest configuration must be used for a fair comparison.

Target metrics:

- win rate
- net P&L
- profit factor
- losses
- average P&L per trade
- losing-day frequency
- consecutive losses
- useful trade frequency

The desired result is a higher win rate and better P&L while preserving
independent VWAP opportunities.

### Tests

The package retains the existing V9.4.x regression tests and adds V10.1
quality-layer tests.


### V10.2 precision regime layer

The second quality stage adds causal market-structure checks before a new setup
is armed:

- price-efficiency ratio to distinguish directional movement from chop
- normalized VWAP slope strength
- short-window VWAP flip density
- maximum normalized distance from VWAP for continuation setups
- opening-session chop veto
- late-session precision gate, with an exception for genuinely strong moves

The 15-point / 8-candle confirmation contract is unchanged. Reversal families
remain more tolerant than ordinary continuation/bounce entries.

This is a **precision attempt**, not a claim that 70% is guaranteed. The full
historical candle replay must be rerun to measure the actual result without
look-ahead or trade-log overfitting.
