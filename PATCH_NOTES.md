# TradingCore v9.8.2

## Overall Web UI polish
- Reworked the application shell into a cleaner professional trading-terminal presentation.
- Added polished TradingCore header/status pills with existing engine state only.
- Refined top navigation into a compact command bar.
- Upgraded sidebar controls, inputs, buttons, expanders, alerts, metrics and data tables.
- Added responsive behavior for narrower screens.
- Kept chart rendering, VWAP, market-data flow, CE/PE premium selection, entry/SL/target logic and live order logic unchanged.
- No additional FYERS/Supabase requests were introduced by the UI layer.

## v9.8.2 UI/Live reliability follow-up — 2026-09-01
- Charts page now merges background execution events before rendering, so a live CE/PE trigger cannot render only the NIFTY marker while hiding the selected premium state.
- Added an immediate premium-leg status card: `SELECTING PREMIUM` while FYERS option selection is in flight, then `PREMIUM ACTIVE` with contract + entry premium once selected.
- Charts connection state now distinguishes `LIVE`, `RECONNECTING`, `FEED BLOCKED`, `CONNECTING`, and `STOPPED` instead of masking reconnects as generic CONNECTING.
- Reconnect state explicitly shows `Auto-recovery active` while the existing FYERS SDK reconnect/gap-repair path is working.
- No changes were made to CE/PE direction mapping, premium-band selection, live order payloads, protection prices, or canonical candle construction.
