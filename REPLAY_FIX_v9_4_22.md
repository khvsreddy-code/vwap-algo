# Replay / marker fix v9.4.22

## Fixed

### 1. Next candle / Run replay controls
The replay slider previously had a persistent widget key while the buttons
changed `st.session_state.replay_cursor`. Streamlit could restore the old
slider widget value on the fragment rerun, immediately undoing the button
change. The slider is now driven directly from `replay_cursor`, so:

- Reset -> candle 1
- Next candle -> exactly +1 candle
- Run replay -> exactly +1 candle per replay tick
- Pause -> stops advancing
- Manual slider movement -> jumps to that candle and pauses

### 2. BUY/SELL markers appearing on one candle
`app.py` already supplied marker times as Unix seconds. `live_chart.py`
converted those integers with `pd.Timestamp(integer)`, which interprets an
integer as nanoseconds. That moved markers to 1970 and caused them to appear
stacked at one edge/candle.

Numeric marker timestamps are now preserved as Unix seconds. Datetime strings
are still parsed normally.

No strategy rules were changed.
