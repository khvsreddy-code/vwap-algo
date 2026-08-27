# Wide / Cumulative Cloud Option-Chain Recorder

The cloud recorder no longer maintains a fixed 20 CE + 20 PE universe.

- FYERS Option Chain is requested at the maximum supported `strikecount=50`.
- Every CE/PE contract returned for the selected (nearest) expiry is registered.
- NIFTY 50 is recorded separately as its own 1-minute instrument.
- When NIFTY moves by about one strike from the current chain center (or approaches the current returned strike-window edge), the recorder refreshes the chain and **adds** newly discovered contracts. Refreshes are throttled to avoid REST request storms.
- Previously discovered option symbols are never unsubscribed by the cloud recorder and their historical rows are never deleted.
- This creates a cumulative option-history dataset in Supabase while staying within the FYERS websocket subscription limit.
- The live trading engine can independently select the nearest 20 CE + 20 PE from the available chain; this does not restrict cloud collection.
- Premium OHLCV comes from the FYERS WebSocket.
- OI / OI change comes from Option Chain snapshots and is attached to the latest valid 1-minute candle for that symbol.
- OI is not fabricated tick-by-tick; FYERS/NSE updates OI at its own cadence.

The cloud recorder is therefore a historical data layer, not the same thing as the live strategy's option-selection layer.
