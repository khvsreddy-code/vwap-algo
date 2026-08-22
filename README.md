# FYERS VWAP Trader V10

## What changed

V10 is a **quality-first continuation of the 9.4.x VWAP engine**, not a blind
increase in confirmation strictness.

The core confirmation rule remains:

- VWAP setup candle arms the trade.
- Confirmation remains **15 NIFTY points** by default.
- Confirmation window remains **8 candles** by default.
- The setup candle cannot confirm itself.
- Historical replay uses candle high/low; live confirmation uses ticks.

### V10 quality layer

The strategy now scores a setup using only information available at the setup
candle:

- price side of VWAP
- VWAP slope relative to ATR
- ADX/trend strength
- candle body / close location
- displacement from VWAP
- recent VWAP flip count
- time-of-day quality

The primary `CLOSE_CROSS` path remains the lowest threshold so the profitable
legacy behaviour is not discarded. Extra setup families are held to higher
quality thresholds:

- `VWAP_RECLAIM`: medium/high quality
- `VWAP_BOUNCE`: high quality
- `VWAP_INTERACTION`: high quality
- `FAILED_CROSS`: **disabled by default** because it can create rapid side
  reversals in chop

Late-session setups from 14:00 IST onward require a higher score rather than
a daily trade quota.

### Important limitation

A 70% win rate cannot honestly be guaranteed from the trade-export CSV alone.
The supplied CSV contains **118 outcomes (69 wins / 49 losses = 58.47%)**, but it
does not contain the 3,225 underlying candles or the setup features needed to
optimize the signal rules without hindsight.

V10 therefore uses the CSV only to identify broad failure concentrations
(e.g. the weaker late-session cluster) and requires the same full FYERS candle
history for final parameter validation.

Do not enable live broker orders solely because a backtest reaches a target.
Walk-forward/out-of-sample validation is still required.

## Chart fix

The persistent Lightweight Charts component now uses an explicit non-zero
height on both the Streamlit component wrapper and the chart host. It also
re-applies the requested height during reruns and resize events, preventing the
small/collapsed chart state seen in the previous build.

## Run

```bash
streamlit run app.py
```

## Default V10 settings

```text
confirmation_points = 15
confirmation_bars   = 8

setup_score_primary_min     = 4
setup_score_reclaim_min     = 5
setup_score_bounce_min      = 7
setup_score_interaction_min = 7

allow_failed_cross = false

late_session_start = 14:00 IST
late_session_min_score = 6

chop_lookback = 8
max_chop_flips = 2
strong_move_atr_fraction = 0.50
range_adx_threshold = 20
```
