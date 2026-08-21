# v9.4.29 — Replay Portfolio Metrics + IST Entry Window

- Replay portfolio shows Total Entries, Total Wins, Total Losses, Win Ratio, and Net P&L.
- Replay/backtest timestamps are displayed in India Standard Time (IST).
- New entries are accepted only from 09:15 through 15:15 IST.
- Existing positions may still exit by SL/target after 15:15; the cutoff blocks new entries only.
- Pending setups are cleared at trading-session boundaries.
- The live engine applies the same 09:15–15:15 IST entry restriction to closed-candle and live-tick entries.
