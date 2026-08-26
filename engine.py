import threading
from queue import Queue, Empty
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import os

from fyers_client import FyersClient
from cloud_data import CloudMarketStore, CloudCandleRecorder, _as_float, _as_int
from strategy import VwapConfirmationEngine, StrategyConfig

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_ENTRY_START_MINUTE = 9 * 60 + 15
DEFAULT_ENTRY_END_MINUTE = 15 * 60 + 15

def _hhmm_to_minute(value, default):
    if value is None:
        return default
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            return int(value[0]) * 60 + int(value[1])
        except Exception:
            return default
    text = str(value).strip()
    try:
        hh, mm = text.split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:
        return default

def _within_entry_window(timestamp, start_minute=DEFAULT_ENTRY_START_MINUTE, end_minute=DEFAULT_ENTRY_END_MINUTE):
    ts = timestamp if isinstance(timestamp, datetime) else datetime.now(IST)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    else:
        ts = ts.astimezone(IST)
    minute = ts.hour * 60 + ts.minute
    return int(start_minute) <= minute <= int(end_minute)



class TradingEngine:
    def __init__(self, client, signal_symbol, resolution, confirmation_points, confirmation_bars,
                 qty, live_trading, option_cfg, protection_cfg, test_live_entry=False,
                 session_start="09:15", session_end="15:15"):
        self.client = client
        self.signal_symbol = signal_symbol
        self.resolution = resolution
        self.qty = int(qty)
        self.live_trading = live_trading
        # Test-live mode exercises the REAL entry decision path (option
        # selection + protection + exact broker order payload) but NEVER calls
        # the FYERS order-placement endpoint.
        self.test_live_entry = bool(test_live_entry) and not bool(live_trading)
        self.option_cfg = option_cfg
        self.protection_cfg = protection_cfg
        self.strategy = VwapConfirmationEngine(StrategyConfig(confirmation_points, confirmation_bars))
        self.session_start_minute = _hhmm_to_minute(session_start, DEFAULT_ENTRY_START_MINUTE)
        self.session_end_minute = _hhmm_to_minute(session_end, DEFAULT_ENTRY_END_MINUTE)
        if self.session_end_minute < self.session_start_minute:
            raise ValueError("Algo end time must be at or after the start time.")
        self.events = Queue()
        self.running = False
        self.thread = None
        self.socket = None
        # Dedicated FYERS v3 order/trade/position WebSocket state. Initialize
        # these explicitly so the first start_order_socket() call is idempotent
        # instead of raising AttributeError before the broker socket is created.
        self.order_socket = None
        self.order_ws_connected = False
        # Order execution is deliberately separated from the market-data callback.
        # FYERS REST calls (option selection/order placement) can be slow; blocking
        # the websocket callback here can otherwise delay subsequent confirmation ticks.
        self.execution_queue = Queue()
        self.execution_thread = None
        self.execution_stop = threading.Event()
        self.history_df = pd.DataFrame()
        self.execution_history = pd.DataFrame()
        self.last_tick = {}
        self.last_execution_tick = {}
        self.last_signal = None
        self.last_order = None
        self.current_vwap = None
        self.signal_count = 0
        self.selected_option = None
        self.protection = None
        self._current_candle = None
        self._last_cum_volume = None
        # Separate option/execution candle state. The selected option receives
        # its own FYERS ticks, so its premium chart must be built from those
        # ticks instead of remaining frozen at the history snapshot taken at
        # entry time.
        self._execution_current_candle = None
        # Reference premium used only to quarantine obviously foreign-symbol
        # ticks from the option chart (e.g. an occasional NIFTY LTP arriving
        # on the shared socket). This is a chart/data-isolation guard, not an
        # execution rule.
        self._option_price_reference = None
        self._option_price_ceiling = None
        self.ws_connected = False
        self.last_ws_event = None
        self.entry_attempts = 0
        self.test_entry_count = 0
        # Local execution ledger used by the chart and portfolio UI. This is
        # intentionally separate from the broker portfolio so an accepted/test/
        # paper execution is visible immediately, even before FYERS portfolio
        # snapshots catch up.
        self.execution_events = []
        self._last_armed_key = None
        self._last_entry_log = None
        self.last_rejection = None
        self.market_data_blocked = False
        self.market_data_error = None
        self._last_error_signature = None
        self._last_error_time = 0.0
        self.market_data_lite = False
        # FYERS owns reconnects for a live data socket. Keep the desired symbol set
        # so a broker-side reconnect can restore the option subscription too.
        self.data_symbols = {self.signal_symbol}
        self.market_data_reconnecting = False
        self._socket_started_at = None
        self._last_transport_notice = 0.0
        self._last_option_subscribe = None
        self._last_tick_monotonic = 0.0
        self._socket_lock = threading.RLock()
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = None
        self._socket_generation = 0
        # Cloud market-data recorder. It is intentionally independent of the
        # chart and strategy so the full option-chain window can be recorded even
        # when only one option is selected for execution.
        self.cloud_store = None
        self.cloud_recorder = None
        self.cloud_data_symbols = set()
        self.cloud_expiry = None
        self.cloud_oi_thread = None
        self.cloud_oi_stop = threading.Event()
        self.cloud_status = "DISABLED"
        self.cloud_last_oi_at = None
        self.cloud_backfill_thread = None
        self.cloud_backfill_stop = threading.Event()
        # Dynamic option-universe manager. The cloud recorder follows NIFTY's
        # current ATM ladder: the full returned PE/CE strike window
        # around the current spot. A roll is triggered only when spot crosses the current
        # near-ATM boundary, not on every tick.
        self._cloud_universe_lock = threading.RLock()
        self._cloud_roll_thread = None
        self._cloud_roll_requested_spot = None
        self._cloud_roll_requested_at = None
        # Option-chain refreshes are intentionally throttled. When spot sits
        # near the edge of the FYERS strike window, every incoming NIFTY tick
        # can otherwise request another REST chain refresh immediately after
        # the previous worker finishes. That can create a request storm and
        # eventually trigger FYERS rate limits.
        self._cloud_last_chain_refresh_at = None
        self._cloud_chain_refresh_cooldown_seconds = 30.0
        self._cloud_pe_atm_strike = None
        self._cloud_ce_atm_strike = None
        self._cloud_chain_min_strike = None
        self._cloud_chain_max_strike = None
        self._cloud_chain_center_strike = None
        self._cloud_chain_strike_step = None

    def _log(self, message, level="info"):
        """Queue a human-readable strategy/execution log for the Streamlit UI."""
        item = {
            "time": datetime.now(IST),
            "level": level,
            "message": str(message),
        }
        self._last_entry_log = item
        self.events.put(("log", item))

    def _record_rejection(self, message, result=None):
        self.last_rejection = {
            "time": datetime.now(IST),
            "message": str(message),
            "result": result,
        }
        self.events.put(("rejection", self.last_rejection))
        self._log(f"ORDER REJECTED • {message}", "error")

    def _log_setup_state(self, source="live"):
        s = self.strategy
        if s.cross_bar is None or s.confirmation_level is None:
            return
        key = (s.cross_bar, s.cross_direction, round(float(s.confirmation_level), 4))
        if key == self._last_armed_key:
            return
        self._last_armed_key = key
        side = "BUY CE" if s.cross_direction == 1 else "BUY PE"
        self._log(
            f"ENTRY ARMED [{source.upper()}] • {side} • {getattr(s, 'cross_type', 'CLOSE_CROSS')} "
            f"• VWAP reference {s.cross_price:.2f} • trigger {s.confirmation_level:.2f} "
            f"• window {s.config.confirmation_bars} candles",
            "arm",
        )

    def _record_trigger_event(self, signal):
        """Persist a strategy trigger before option selection/order handling.

        The chart and local portfolio ledger must show the signal even when the
        subsequent option-selection or broker step fails.  This event is the
        single source of truth for the CE/PE marker; execute_signal() enriches it
        with the selected contract and final status.
        """
        signal_time = signal.get("time") or datetime.now(IST)
        try:
            chart_time = self._candle_start(pd.Timestamp(signal_time))
        except Exception:
            chart_time = pd.Timestamp(signal_time)
        event = {
            "event_id": uuid.uuid4().hex,
            "execution_time": datetime.now(IST),
            "entry_time": signal_time,
            "chart_time": chart_time,
            "side": "BUY",
            "signal_side": signal["side"],
            "option_type": "CE" if signal["side"] == "BUY" else "PE",
            "symbol": "",
            "strike": None,
            "entry": float(signal.get("entry", 0.0)),
            "trigger_price": float(signal.get("entry", 0.0)),
            "quantity": int(self.qty),
            "lots": 1,
            "status": "TRIGGERED",
            "bars_since_cross": int(signal.get("bars_since_cross", 0)),
            "cross_price": float(signal.get("cross_price", 0.0)),
            "confirmation_level": float(signal.get("confirmation_level", signal.get("entry", 0.0))),
            "cross_type": str(signal.get("cross_type") or "CLOSE_CROSS"),
            "setup_quality": str(signal.get("setup_quality") or signal.get("cross_type") or "CLOSE_CROSS"),
        }
        self.execution_events.append(event)
        self.execution_events = self.execution_events[-100:]
        self.events.put(("execution", dict(event)))
        return event

    def _update_trigger_event(self, event, **updates):
        if event is None:
            return
        event.update(updates)
        # Keep the same dict object in the engine list so the UI sees the latest
        # option/status details on the next fragment rerun.
        self.execution_events = self.execution_events[-100:]
        self.events.put(("execution", dict(event)))

    def load_history(self, days=31):
        df = self.client.history(self.signal_symbol, self.resolution, days)
        # Keep the REST candles intact for the chart. The strategy itself
        # decides which candle is closed/eligible; deleting the last row here
        # could turn a one-candle response into an empty chart.
        if not df.empty:
            df = df.copy()
        self.history_df = VwapConfirmationEngine.prepare(df) if not df.empty else df
        if not self.history_df.empty:
            v = self.history_df.iloc[-1].get("vwap")
            self.current_vwap = None if pd.isna(v) else float(v)
            # Important: rebuild the pending VWAP setup from closed history,
            # but only from candles inside the configured algo session. VWAP
            # itself is still calculated from the complete history.
            # Without this, a cross after the user's end time could leak into
            # the next live session.
            seed_df = self.history_df.copy()
            if "datetime" in seed_df.columns:
                _ts = pd.to_datetime(seed_df["datetime"])
                if getattr(_ts.dt, "tz", None) is None:
                    _ts = _ts.dt.tz_localize(IST)
                else:
                    _ts = _ts.dt.tz_convert(IST)
                _mins = _ts.dt.hour * 60 + _ts.dt.minute
                seed_df = seed_df[(_mins >= self.session_start_minute) & (_mins <= self.session_end_minute)].copy()
            self.strategy.seed_from_history(seed_df)
            self._log_setup_state("history")
        return self.history_df

    def _timeframe_minutes(self):
        return max(1, int(self.resolution))

    def _candle_start(self, ts):
        ts = ts.astimezone(IST)
        minutes = self._timeframe_minutes()
        total = ts.hour * 60 + ts.minute
        # Anchor intraday candles to the user-selected algo start. This keeps
        # the first candle at the configured start (e.g. 09:15-09:20 on 5m),
        # instead of flooring to midnight (which incorrectly makes 10m candles
        # start at 09:10, 09:20, ...). Before the session, retain the natural
        # bucket only for chart/history purposes.
        anchor = self.session_start_minute
        if total >= anchor:
            bucket = anchor + ((total - anchor) // minutes) * minutes
        else:
            bucket = (total // minutes) * minutes
        return ts.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)

    def _process_tick_into_candle(self, message):
        ts_epoch = message.get("last_traded_time") or message.get("timestamp")
        ts = datetime.now(IST) if ts_epoch is None else datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc).astimezone(IST)
        ltp = message.get("ltp")
        if ltp is None:
            return
        ltp = float(ltp)
        start = self._candle_start(ts)
        cum_vol = message.get("vol_traded_today")
        try: cum_vol = float(cum_vol) if cum_vol is not None else None
        except (TypeError, ValueError): cum_vol = None
        if cum_vol is not None and self._last_cum_volume is not None:
            tick_volume = max(0.0, cum_vol - self._last_cum_volume)
        else:
            tick_volume = 0.0
        if cum_vol is not None:
            self._last_cum_volume = cum_vol

        if self._current_candle is None:
            self._current_candle = {"datetime":start,"open":ltp,"high":ltp,"low":ltp,"close":ltp,"volume":tick_volume,
                                     "_algo_session_first": (start.hour * 60 + start.minute) == self.session_start_minute}
            return dict(self._current_candle)
        if start > self._current_candle["datetime"]:
            self._accept_closed_live_candle(pd.Series(self._current_candle))
            self._current_candle = {"datetime":start,"open":ltp,"high":ltp,"low":ltp,"close":ltp,"volume":tick_volume,
                                     "_algo_session_first": (start.hour * 60 + start.minute) == self.session_start_minute}
        elif start == self._current_candle["datetime"]:
            self._current_candle["high"] = max(self._current_candle["high"], ltp)
            self._current_candle["low"] = min(self._current_candle["low"], ltp)
            self._current_candle["close"] = ltp
            self._current_candle["volume"] += tick_volume
        return dict(self._current_candle)

    def _accept_closed_live_candle(self, candle):
        # Keep the session-first flag for strategy evaluation, but do not rely
        # on it as a market-data column after the candle is stored.
        candle = candle.copy()
        self.history_df = pd.concat([self.history_df, pd.DataFrame([candle])], ignore_index=True)
        self.history_df = self.history_df.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
        prepared = VwapConfirmationEngine.prepare(self.history_df)
        row = prepared.iloc[-1]
        self.history_df = prepared
        v = row.get("vwap")
        if pd.notna(v):
            self.current_vwap = float(v)
        # If the index feed supplies no volume, keep the last valid session VWAP
        # rather than replacing it with NaN. This does not invent a VWAP value.
        self.events.put(("candle", row.to_dict()))

        before = (self.strategy.cross_bar, self.strategy.cross_direction, self.strategy.confirmation_level)
        signal = self.strategy.process_closed_candle(row)

        if self.strategy.cross_bar is not None:
            self._log_setup_state("closed candle")

        # If an old setup expired/replaced, make that visible instead of silently
        # leaving the user wondering why no order happened.
        after = (self.strategy.cross_bar, self.strategy.cross_direction, self.strategy.confirmation_level)
        if before[0] is not None and after[0] is None and signal is None:
            self._last_armed_key = None
            self._log("Pending VWAP entry setup expired/cleared.", "info")

        if signal:
            signal_time = row.get("datetime")
            if _within_entry_window(signal_time, self.session_start_minute, self.session_end_minute):
                self.last_signal = signal
                self.signal_count += 1
                self.events.put(("signal", signal))
                self._log(
                    f"ENTRY TRIGGERED • BUY {'CE' if signal['side'] == 'BUY' else 'PE'} "
                    f"• price {signal['entry']:.2f} "
                    f"• VWAP cross close {signal['cross_price']:.2f} • "
                    f"bar {signal['bars_since_cross']}",
                    "entry",
                )
                trigger_event = self._record_trigger_event(signal)
                self.execute_signal(signal, trigger_event=trigger_event)
                # `trade_active` is a per-setup duplicate guard, not a lifetime
                # position lock. There is no strategy-level exit callback in the
                # live engine, so leaving it True would permanently suppress all
                # later intrabar confirmations after the first entry.
                self.strategy.reset_trade()
            else:
                self.strategy.reset_trade()
                try:
                    self.strategy._clear_setup()
                except Exception:
                    pass
                self._log("ENTRY BLOCKED • outside configured algo entry window (IST).", "info")

    @staticmethod
    def _select_cloud_option_universe(chain, spot, expiry, underlying, signal_symbol):
        """Build the maximum practical FYERS chain returned around current ATM.

        The cloud recorder is deliberately wider than the live strategy universe.
        Every CE/PE contract returned by FYERS for the requested strike window is
        registered and recorded. The live strategy can independently choose its
        nearest 20 CE + 20 PE later. Historical contracts are never removed.
        """
        try:
            spot_f = float(spot)
        except (TypeError, ValueError):
            raise RuntimeError(f"Invalid NIFTY spot for option universe: {spot!r}")

        meta = {
            signal_symbol: {
                "symbol": signal_symbol,
                "underlying": underlying,
                "expiry": None,
                "strike": None,
                "option_type": None,
            }
        }
        selected_symbols = set()
        selected_items = []
        strikes = []
        seen = set()
        for item in chain or []:
            typ = str(item.get("option_type") or "").upper()
            symbol = str(item.get("symbol") or "").strip()
            if typ not in {"CE", "PE"} or not symbol or symbol in seen:
                continue
            try:
                strike_f = float(item.get("strike_price"))
            except (TypeError, ValueError):
                continue
            seen.add(symbol)
            strikes.append(strike_f)
            meta[symbol] = {
                "symbol": symbol,
                "underlying": underlying,
                "expiry": str(expiry.get("expiry")) if isinstance(expiry, dict) else str(expiry),
                "strike": strike_f,
                "option_type": typ,
            }
            selected_symbols.add(symbol)
            selected_items.append(item)

        if not selected_symbols:
            raise RuntimeError("FYERS returned no CE/PE contracts for the cloud recorder.")

        return meta, selected_symbols, selected_items, (min(strikes) if strikes else None), (max(strikes) if strikes else None)

    def _fetch_cloud_chain(self):
        """Fetch the maximum practical current-expiry chain FYERS exposes.

        FYERS caps the Option Chain API strikecount at 50. We intentionally use
        the maximum supported value for cloud collection. The returned CE/PE
        contracts are all recorded; there is no 20+20 cloud selection here.
        """
        strikecount = 50
        first = self.client.option_chain(
            self.option_cfg["underlying"],
            strikecount=strikecount,
            greeks=False,
        )
        data = first.get("data", first) if isinstance(first, dict) else {}
        expiries = data.get("expiryData", []) or []
        if not expiries:
            raise RuntimeError("FYERS returned no option expiry for the cloud recorder.")
        expiry = expiries[0]
        chain_resp = self.client.option_chain(
            self.option_cfg["underlying"],
            strikecount=strikecount,
            timestamp=expiry.get("expiry"),
            greeks=False,
        )
        chain_data = chain_resp.get("data", chain_resp) if isinstance(chain_resp, dict) else {}
        chain = chain_data.get("optionsChain", []) or []
        if not chain:
            raise RuntimeError("FYERS returned an empty option chain for the cloud recorder.")
        return expiry, chain

    def _current_nifty_spot_for_cloud(self, chain):
        # Prefer the underlying row returned by option-chain; if it is absent,
        # use the live NIFTY tick, then REST quote as a final fallback.
        for item in chain:
            if str(item.get("option_type") or "") == "" and item.get("ltp") is not None:
                try:
                    return float(item.get("ltp"))
                except (TypeError, ValueError):
                    pass
        try:
            tick = self.last_tick.get("ltp") if isinstance(self.last_tick, dict) else None
            if tick is not None:
                return float(tick)
        except (TypeError, ValueError):
            pass
        q = self.client.quotes([self.option_cfg["underlying"]])
        d = q.get("d", []) if isinstance(q, dict) else []
        if d and isinstance(d[0], dict):
            v = d[0].get("v", d[0])
            if v.get("lp") is not None:
                return float(v.get("lp"))
        raise RuntimeError("Could not determine NIFTY spot for the 40-contract recorder.")

    def _configure_cloud_recorder(self):
        """Connect Supabase and start a wide, cumulative NIFTY + option-chain recorder."""
        url = os.getenv("SUPABASE_URL", "").strip()
        key = (os.getenv("SUPABASE_SECRET_KEY", "").strip()
               or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())
        enabled = bool(url and key)
        if not enabled:
            self.cloud_status = "NOT_CONFIGURED"
            self.events.put(("status", "CLOUD_DATA_NOT_CONFIGURED"))
            return

        required = os.getenv("CLOUD_DATA_REQUIRED", "1").strip().lower() not in {"0", "false", "no"}
        try:
            store = CloudMarketStore(url, key)
            health = store.health_check()
            if not health.get("ok"):
                raise RuntimeError("Supabase health check failed.")

            expiry, chain = self._fetch_cloud_chain()
            spot = self._current_nifty_spot_for_cloud(chain)
            meta, selected_symbols, selected_items, pe_atm, ce_atm = self._select_cloud_option_universe(
                chain, spot, expiry, self.option_cfg["underlying"], self.signal_symbol
            )
            recorder = CloudCandleRecorder(store, meta)
            recorder.start()  # real Supabase write of the initial instrument set
            self.cloud_store = store
            self.cloud_recorder = recorder
            self.cloud_data_symbols = selected_symbols
            self.cloud_expiry = expiry
            with self._cloud_universe_lock:
                self._cloud_last_chain_refresh_at = datetime.now(IST)
            self._cloud_pe_atm_strike = pe_atm
            self._cloud_ce_atm_strike = ce_atm
            self._cloud_chain_min_strike = pe_atm
            self._cloud_chain_max_strike = ce_atm
            _, _, self._cloud_chain_center_strike, self._cloud_chain_strike_step = (
                self._cloud_chain_geometry(chain, spot)
            )
            self.data_symbols.update(selected_symbols)
            self.cloud_status = "LIVE"
            self.events.put(("status", f"CLOUD_DATA_LIVE:{len(selected_symbols)}_OPTIONS"))

            self._apply_cloud_oi_snapshots(selected_items, selected_symbols)
            self.cloud_oi_stop.clear()
            self.cloud_oi_thread = threading.Thread(
                target=self._cloud_oi_loop,
                name="fyers-cloud-oi-snapshot",
                daemon=True,
            )
            self.cloud_oi_thread.start()
            self.cloud_backfill_stop.clear()
            self.cloud_backfill_thread = threading.Thread(
                target=self._cloud_backfill_today,
                name="fyers-cloud-history-backfill",
                daemon=True,
            )
            self.cloud_backfill_thread.start()
        except Exception as exc:
            self.cloud_status = "ERROR"
            self.events.put(("error", f"Cloud data startup failed: {exc}"))
            if required:
                raise RuntimeError(
                    "Cloud data is required but Supabase/option-chain setup failed. "
                    f"Nothing was started with a false 'saved' state: {exc}"
                ) from exc

    def _apply_cloud_oi_snapshots(self, items, selected_symbols):
        if not self.cloud_recorder:
            return
        selected = set(selected_symbols or set())
        for item in items or []:
            symbol = str(item.get("symbol") or "").strip()
            if symbol in selected:
                self.cloud_recorder.set_oi_snapshot(
                    symbol,
                    oi=item.get("oi"),
                    oi_change=item.get("oich"),
                    prev_oi=item.get("prev_oi"),
                )

    def _cloud_universe_needs_roll(self, spot):
        try:
            spot = float(spot)
        except (TypeError, ValueError):
            return False

        lo = self._cloud_chain_min_strike
        hi = self._cloud_chain_max_strike
        center = self._cloud_chain_center_strike
        step = self._cloud_chain_strike_step
        if lo is None or hi is None or center is None:
            return True

        # The cloud universe is cumulative: refresh discovery as the ATM moves,
        # but NEVER retire/unsubscribe old cloud symbols. One strike of movement
        # is enough to request the latest 50-strike FYERS window; the 30-second
        # cooldown below prevents a REST request storm during fast moves.
        step = max(1.0, float(step or 50.0))
        if abs(spot - float(center)) >= step:
            return True

        # Also refresh if price is approaching the returned window edge. This
        # protects against an unusually asymmetric FYERS response.
        margin = max(step, (float(hi) - float(lo)) * 0.05)
        return spot <= float(lo) + margin or spot >= float(hi) - margin

    @staticmethod
    def _cloud_chain_geometry(chain, spot):
        strikes = sorted({
            float(item.get("strike_price"))
            for item in (chain or [])
            if str(item.get("option_type") or "").upper() in {"CE", "PE"}
            and item.get("strike_price") is not None
        })
        if not strikes:
            return None, None, None, None
        center = min(strikes, key=lambda x: abs(x - float(spot)))
        diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
        step = min(diffs) if diffs else 50.0
        return min(strikes), max(strikes), center, step

    def _request_cloud_universe_roll(self, spot):
        """Schedule a non-blocking option-universe refresh from the NIFTY tick path."""
        if not self.running or self.cloud_recorder is None:
            return
        try:
            spot = float(spot)
        except (TypeError, ValueError):
            return
        with self._cloud_universe_lock:
            if not self._cloud_universe_needs_roll(spot):
                return
            now = datetime.now(IST)
            if self._cloud_last_chain_refresh_at is not None:
                elapsed = (now - self._cloud_last_chain_refresh_at).total_seconds()
                if elapsed < self._cloud_chain_refresh_cooldown_seconds:
                    # Keep the latest spot so the worker can use it once the
                    # cooldown expires, but never start a REST request storm.
                    self._cloud_roll_requested_spot = spot
                    self._cloud_roll_requested_at = now
                    return
            self._cloud_roll_requested_spot = spot
            self._cloud_roll_requested_at = now
            if self._cloud_roll_thread is not None and self._cloud_roll_thread.is_alive():
                return
            self._cloud_roll_thread = threading.Thread(
                target=self._cloud_roll_worker,
                name="fyers-cloud-universe-roll",
                daemon=True,
            )
            self._cloud_roll_thread.start()

    def _cloud_roll_worker(self):
        """Refresh the wide chain and ADD newly discovered contracts only.

        We intentionally never unsubscribe retired cloud symbols. This creates a
        cumulative historical option universe in Supabase, bounded only by FYERS
        websocket subscription capacity.
        """
        while self.running and self.cloud_recorder is not None:
            with self._cloud_universe_lock:
                spot = self._cloud_roll_requested_spot
                self._cloud_roll_requested_spot = None
            if spot is None:
                return
            try:
                expiry, chain = self._fetch_cloud_chain()
                try:
                    latest = float(self.last_tick.get("ltp")) if self.last_tick else None
                    if latest is not None:
                        spot = latest
                except (TypeError, ValueError):
                    pass
                meta, discovered, selected_items, chain_min, chain_max = self._select_cloud_option_universe(
                    chain, spot, expiry, self.option_cfg["underlying"], self.signal_symbol
                )
                old_symbols = set(self.cloud_data_symbols)
                added = discovered - old_symbols

                if added:
                    self.cloud_recorder.register_instruments({symbol: meta[symbol] for symbol in added})
                    with self._socket_lock:
                        sock = self.socket
                    if sock is not None:
                        try:
                            self.client.subscribe_data_socket(sock, sorted(added), "SymbolUpdate")
                        except Exception as exc:
                            self._emit_error_once(
                                f"Cloud option subscribe retryable: {exc}",
                                key=("cloud-sub", str(exc)),
                                cooldown=60,
                            )
                    self.data_symbols.update(added)
                    self.cloud_data_symbols.update(added)
                    self._apply_cloud_oi_snapshots(selected_items, discovered)
                    # Do not backfill every newly discovered option through
                    # FYERS History. A wide chain can contain ~100 contracts,
                    # and the History helper uses multiple REST requests per
                    # contract. That creates a large request burst and the
                    # legitimate "no_data" responses were flooding the terminal.
                    # Live option candles are collected from the WebSocket;
                    # OI is collected separately from Option Chain snapshots.
                else:
                    self._apply_cloud_oi_snapshots(selected_items, discovered)

                self.cloud_expiry = expiry
                with self._cloud_universe_lock:
                    self._cloud_last_chain_refresh_at = datetime.now(IST)
                self._cloud_chain_min_strike = chain_min
                self._cloud_chain_max_strike = chain_max
                _, _, self._cloud_chain_center_strike, self._cloud_chain_strike_step = (
                    self._cloud_chain_geometry(chain, spot)
                )
                # Keep these for diagnostics/backward compatibility; they now mean
                # the current chain's lower/upper strike bounds rather than a 20+20 ATM ladder.
                self._cloud_pe_atm_strike = chain_min
                self._cloud_ce_atm_strike = chain_max

                self.events.put((
                    "status",
                    f"CLOUD_CHAIN_REFRESHED:{len(self.cloud_data_symbols)}_OPTIONS"
                    f":ADDED={len(added)}:SPOT={float(spot):.2f}"
                    f":RANGE={chain_min:g}-{chain_max:g}"
                ))
            except Exception as exc:
                self._emit_error_once(
                    f"Cloud option-chain refresh failed; keeping previous chain: {exc}",
                    key=("cloud-chain-refresh", str(exc)),
                    cooldown=30,
                )
            with self._cloud_universe_lock:
                if self._cloud_roll_requested_spot is None:
                    return

    def _cloud_backfill_symbols(self, symbols):
        """Compatibility no-op for retired callers.

        Option contracts are intentionally NOT historical-backfilled here.
        A wide FYERS chain can contain roughly 100 CE/PE symbols, while the
        History endpoint is chunked into multiple REST requests per symbol.
        Backfilling that entire universe at startup/roll causes unnecessary
        request bursts and repeated FYERS ``no_data`` responses. Live option
        candles are recorded from the WebSocket instead.
        """
        return None

    def _cloud_backfill_today(self):
        """Backfill only NIFTY's missing 1-minute candles.

        Option history is deliberately excluded. The cloud recorder is a live
        option-chain collector; requesting FYERS History for every option in a
        wide chain can generate hundreds/thousands of REST calls and many
        legitimate ``no_data`` responses. Options begin recording from the
        live WebSocket, while OI/OI-change snapshots come from Option Chain.
        """
        try:
            if self.cloud_backfill_stop.is_set() or not self.running:
                return

            today = datetime.now(IST).date()
            symbol = self.signal_symbol
            try:
                df = self.client.history(symbol, "1", days=3, oi_flag=False)
                if df is None or df.empty:
                    return

                dts = pd.to_datetime(df["datetime"])
                if dts.dt.tz is None:
                    dts = dts.dt.tz_localize(IST)
                else:
                    dts = dts.dt.tz_convert(IST)
                df = df.copy()
                df["datetime"] = dts
                day = df[df["datetime"].dt.date == today].copy()
                if day.empty:
                    return

                rows = []
                meta = self.cloud_recorder.instrument_meta.get(symbol, {}) if self.cloud_recorder else {}
                for _, r in day.iterrows():
                    rows.append({
                        **meta,
                        "candle_start": pd.Timestamp(r["datetime"]).isoformat(),
                        "open": _as_float(r.get("open")),
                        "high": _as_float(r.get("high")),
                        "low": _as_float(r.get("low")),
                        "close": _as_float(r.get("close")),
                        "ltp": _as_float(r.get("close")),
                        "volume": _as_int(r.get("volume")) or 0,
                        "oi": None,
                        "oi_change": None,
                        "prev_oi": None,
                        "oi_snapshot_at": None,
                        "source": "fyers_history_backfill",
                    })
                if rows and self.cloud_store is not None:
                    self.cloud_store.upsert_candles(rows)
            except Exception as exc:
                # NIFTY history is useful recovery data, but it must never stop
                # the live WebSocket. Keep this as one bounded diagnostic event.
                self._emit_error_once(
                    f"Cloud NIFTY historical backfill skipped: {exc}",
                    key=("cloud-nifty-backfill", str(exc)),
                    cooldown=120,
                )
        finally:
            self.events.put(("status", "CLOUD_BACKFILL_DONE"))

    def _cloud_oi_loop(self):
        while self.running and not self.cloud_oi_stop.wait(60.0):
            try:
                if not self.cloud_recorder or not self.cloud_expiry:
                    continue
                response = self.client.option_chain(
                    self.option_cfg["underlying"],
                    strikecount=50,
                    timestamp=self.cloud_expiry.get("expiry"),
                    greeks=False,
                )
                data = response.get("data", response) if isinstance(response, dict) else {}
                chain = data.get("optionsChain", []) or []
                wanted = self.cloud_data_symbols
                for item in chain:
                    symbol = str(item.get("symbol") or "").strip()
                    if symbol in wanted:
                        self.cloud_recorder.set_oi_snapshot(
                            symbol,
                            oi=item.get("oi"),
                            oi_change=item.get("oich"),
                            prev_oi=item.get("prev_oi"),
                        )
                self.cloud_last_oi_at = datetime.now(IST)
            except Exception as exc:
                # OI polling failure must not kill the price websocket. The
                # recorder keeps the previous OI snapshot and retries next minute.
                self._emit_error_once(
                    f"Cloud OI snapshot failed; retrying: {exc}",
                    key=("cloud-oi", str(exc)),
                    cooldown=120,
                )

    def start(self):
        if self.running:
            return
        # Websocket reconnects should rebuild only the *partial* market-data
        # candle. Keep the strategy state/history intact so a valid armed setup
        # survives a brief disconnect, but do not reuse stale OHLC or cumulative
        # volume from the previous socket session.
        self._current_candle = None
        self._last_cum_volume = None
        self.last_tick = {}
        self.last_execution_tick = {}
        self.ws_connected = False
        self.market_data_reconnecting = True
        self.running = True
        self._start_execution_worker()
        if self.history_df.empty:
            try:
                self.load_history()
            except Exception as exc:
                # Historical REST bootstrap is also performed by the chart
                # directly. A history outage must never prevent the live socket
                # from starting or crash the Streamlit session.
                self._emit_error_once(
                    f"Initial history unavailable; chart will retry REST history: {exc}",
                    key=("history_bootstrap", str(exc)),
                    cooldown=120,
                )
        # Configure cloud recording BEFORE the websocket is created so the
        # socket subscribes to NIFTY + the full currently returned CE/PE chain
        # from its first authenticated connection. The cloud universe grows
        # cumulatively as NIFTY moves; old symbols are never unsubscribed.
        self._configure_cloud_recorder()
        self._watchdog_stop.clear()
        self.thread = threading.Thread(
            target=self._run_socket,
            name="fyers-vwap-market-data",
            daemon=True,
        )
        self.thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._socket_watchdog,
            name="fyers-vwap-market-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        self.events.put(("status", "Engine started"))

    def stop(self):
        self.running = False
        self._watchdog_stop.set()
        self.execution_stop.set()
        sock = self.socket
        self.socket = None
        try:
            if sock is not None and hasattr(sock, "close_connection"):
                sock.close_connection()
            elif sock is not None and hasattr(sock, "close"):
                sock.close()
        except Exception:
            pass
        worker = self.execution_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=0.5)
        self.execution_thread = None
        self.cloud_oi_stop.set()
        self.cloud_backfill_stop.set()
        if self.cloud_oi_thread is not None and self.cloud_oi_thread.is_alive():
            self.cloud_oi_thread.join(timeout=1.0)
        self.cloud_oi_thread = None
        if self.cloud_recorder is not None:
            try:
                self.cloud_recorder.stop(flush=True)
            except Exception as exc:
                self._emit_error_once(
                    f"Cloud data final flush failed: {exc}",
                    key=("cloud-stop", str(exc)),
                    cooldown=30,
                )
        self.ws_connected = False
        self.market_data_reconnecting = False
        self.events.put(("status", "STOPPED"))

    def start_order_socket(self):
        """Start FYERS v3 order/trade/position websocket once."""
        if self.order_socket is not None:
            return self.order_socket
        if not self.running:
            return None

        def on_orders(msg):
            self.order_ws_connected = True
            self.events.put(("order_update", {"kind": "orders", "data": msg, "time": datetime.now(IST)}))

        def on_trades(msg):
            self.order_ws_connected = True
            self.events.put(("order_update", {"kind": "trades", "data": msg, "time": datetime.now(IST)}))

        def on_positions(msg):
            self.order_ws_connected = True
            self.events.put(("order_update", {"kind": "positions", "data": msg, "time": datetime.now(IST)}))

        def on_general(msg):
            self.order_ws_connected = True
            self.events.put(("order_update", {"kind": "general", "data": msg, "time": datetime.now(IST)}))

        try:
            self.order_socket = self.client.start_order_socket(
                on_orders=on_orders,
                on_trades=on_trades,
                on_positions=on_positions,
                on_general=on_general,
            )
            if self.order_socket is None:
                self.order_ws_connected = False
                self.events.put(("status", "ORDER_WS_UNAVAILABLE"))
                return None
            self.order_ws_connected = True
            self.events.put(("status", "ORDER_WS_CONNECTED"))
            return self.order_socket
        except Exception as exc:
            self.order_socket = None
            self.order_ws_connected = False
            # Order updates are supplemental. Never take down the market-data
            # engine because the optional order socket is unavailable.
            self._emit_error_once(
                f"Order WebSocket unavailable: {exc}",
                key=("order_ws", str(exc)),
                cooldown=300,
            )
            return None

    def stop_order_socket(self):
        sock = self.order_socket
        self.order_socket = None
        self.order_ws_connected = False
        if sock is not None:
            try:
                if hasattr(sock, "close_connection"):
                    sock.close_connection()
                elif hasattr(sock, "close"):
                    sock.close()
            except Exception:
                pass

    def _on_ws_connect(self, sock=None):
        # Keep the live socket reference as soon as FYERS authenticates. The
        # watchdog and live option subscription both use this object.
        if sock is not None:
            with self._socket_lock:
                self.socket = sock
        # The FYERS SDK reconnects the same socket when reconnect=True. Re-apply
        # the desired symbols after every successful authentication so the
        # selected option feed survives a transient network interruption.
        self.last_ws_event = datetime.now(IST)
        self.market_data_reconnecting = False
        self._socket_started_at = None
        self._last_transport_notice = 0.0
        self._last_option_subscribe = None
        self._last_tick_monotonic = 0.0
        if sock is not None:
            extra = sorted(self.data_symbols - {self.signal_symbol})
            if extra:
                try:
                    self.client.subscribe_data_socket(sock, extra, "SymbolUpdate")
                except Exception as exc:
                    self.market_data_error = str(exc)
        self.events.put(("status", "AUTHENTICATED"))

    def _on_ws_close(self, msg=None):
        self.ws_connected = False
        self.market_data_reconnecting = bool(self.running and not self.market_data_blocked)
        self.last_ws_event = datetime.now(IST)

    def _emit_error_once(self, message, key=None, cooldown=60):
        import time
        key = key or str(message)
        now = time.monotonic()
        if key != self._last_error_signature or now - self._last_error_time >= cooldown:
            self._last_error_signature = key
            self._last_error_time = now
            self.events.put(("error", str(message)))

    def _on_ws_error(self, msg=None):
        import time
        self.last_ws_event = datetime.now(IST)
        self.market_data_error = msg

        code = msg.get("code") if isinstance(msg, dict) else None
        text = str(msg)
        lower = text.lower()

        if code == 11011 or "subscription failed" in lower:
            # A broker-side subscription rejection is not fixed by reconnecting.
            self.market_data_blocked = True
            self.ws_connected = False
            self.market_data_reconnecting = False
            self._emit_error_once(
                "FYERS rejected the market-data subscription (11011). "
                "Reconnect is paused. Check the App ID/token pair, symbol and "
                "market-data permission, then press Connect again.",
                key=("sub", code, text),
                cooldown=300,
            )
            return

        if any(x in lower for x in (
            "remote host was lost", "connection to remote host was lost",
            "connection reset", "connection aborted", "timed out",
            "1006", "connection closed"
        )):
            # Transport errors are handled by FYERS reconnect=True. Do not push
            # them into the Streamlit event queue; doing so creates needless
            # reruns/toasts and was the source of the visible error storm.
            self.ws_connected = False
            self.market_data_reconnecting = True
            return

        self.ws_connected = False
        self.market_data_reconnecting = True
        self._emit_error_once(
            f"Market data error: {msg}",
            key=("other", code, text),
            cooldown=60,
        )

    def _socket_watchdog(self):
        """Recover a market-data socket that is connected but no longer delivering ticks.

        FYERS has SDK-level reconnects, but a dead transport can occasionally remain
        stuck long enough that the SDK does not return to ``connect()``.  The
        watchdog only acts during the cash/index market window and only when the
        engine has previously received live data, so it does not churn the socket
        while the market is closed or during the initial history bootstrap.
        """
        import time

        while self.running and not self._watchdog_stop.wait(2.0):
            if self.market_data_blocked or not self.ws_connected:
                continue

            tick_age = self.tick_age_seconds()
            if tick_age is None or tick_age <= 12:
                continue

            now = datetime.now(IST)
            minute = now.hour * 60 + now.minute
            # NIFTY cash/index feed is expected during the regular session.
            if minute < 9 * 60 or minute > 16 * 60:
                continue

            sock = self.socket
            if sock is None:
                continue

            self.ws_connected = False
            self.market_data_reconnecting = True
            self._emit_error_once(
                f"Market data stale for {tick_age:.0f}s; forcing a clean reconnect…",
                key=("stale",),
                cooldown=30,
            )
            try:
                if hasattr(sock, "close_connection"):
                    sock.close_connection()
                elif hasattr(sock, "close"):
                    sock.close()
            except Exception:
                pass

    def _run_socket(self):
        """Own one long-lived market-data socket and recreate it after hard exits.

        The FYERS SDK still performs its own transient reconnects.  This outer
        loop is only for the case where the SDK's socket/keep_running() returns
        after a transport failure.  A fresh socket is created each time instead
        of reusing a dead object.
        """
        import time

        retry_delay = 2.0
        max_retry_delay = 15.0

        while self.running and not self.market_data_blocked:
            sock = None
            try:
                self.market_data_reconnecting = True
                with self._socket_lock:
                    self._socket_generation += 1
                    generation = self._socket_generation

                sock = self.client.start_data_socket(
                    sorted(self.data_symbols),
                    on_message=self._on_tick,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                    on_connect=self._on_ws_connect,
                    lite_mode=self.market_data_lite,
                    queue_interval_ms=50,
                )

                # start_data_socket() returns only after the SDK socket
                # lifecycle has actually ended. This is a reconnect boundary,
                # not a user-facing error. The previous code emitted a red
                # "socket ended; reconnecting" event on every normal lifecycle
                # return and immediately closed the socket a second time.
                if self.running and not self.market_data_blocked:
                    self.ws_connected = False
                    self.market_data_reconnecting = True
            except Exception as exc:
                self.ws_connected = False
                self.market_data_reconnecting = True
                text = str(exc)
                lower = text.lower()

                # Authentication/subscription failures will not be fixed by
                # reconnecting forever.  Transport failures are retryable.
                if any(x in lower for x in (
                    "invalid access token",
                    "authentication failed",
                    "unauthorized",
                    "invalid token",
                    "-16",
                    "subscription failed",
                    "11011",
                )):
                    self.market_data_blocked = True
                    self.market_data_reconnecting = False
                    self._emit_error_once(
                        f"Market data authentication/subscription failed: {exc}",
                        key=("auth", text),
                        cooldown=300,
                    )
                else:
                    self._emit_error_once(
                        f"Market data socket failed: {exc}",
                        key=("socket", text),
                        cooldown=30,
                    )
            finally:
                with self._socket_lock:
                    if self.socket is sock:
                        self.socket = None

                # The client wrapper has already observed the socket lifecycle
                # end. Do not close the returned object again; that close raced
                # FYERS' own reconnect machinery in the broken version.
                if self.running and not self.market_data_blocked:
                    # Back off between hard socket recreations, but reset the
                    # delay after a successful connection/tick.
                    try:
                        time.sleep(retry_delay)
                    except Exception:
                        pass
                    if self._last_tick_monotonic:
                        retry_delay = 2.0
                    else:
                        retry_delay = min(max_retry_delay, retry_delay * 1.5)

        self.ws_connected = False
        self.market_data_reconnecting = False

    def _on_tick(self, message):
        if not isinstance(message, dict):
            return
        symbol = str(message.get("symbol") or "")
        ltp = message.get("ltp")
        if ltp is None:
            return

        now = datetime.now(IST)
        ltp = float(ltp)
        self.ws_connected = True
        self.market_data_reconnecting = False
        self.last_ws_event = now
        import time as _time
        self._last_tick_monotonic = _time.monotonic()

        # Cloud recorder sees ONLY explicitly registered symbols. It runs
        # independently of the strategy so every tracked CE/PE is persisted.
        if self.cloud_recorder is not None:
            try:
                self.cloud_recorder.on_tick(message)
            except Exception as exc:
                self._emit_error_once(
                    f"Cloud candle recorder error: {exc}",
                    key=("cloud-tick", str(exc)),
                    cooldown=60,
                )

        # The cloud option universe follows NIFTY. Only trigger a REST chain
        # refresh when spot crosses the current near-ATM boundary; the refresh
        # itself runs off-thread so a REST call can never block the websocket.
        if symbol.strip() == self.signal_symbol and self.cloud_recorder is not None:
            self._request_cloud_universe_roll(ltp)

        # The same socket carries both NIFTY and the selected option. Never let
        # an option tick overwrite the NIFTY strategy/chart tick.
        # The market-data socket is shared by NIFTY and the selected option.
        # Route ONLY by the explicit FYERS symbol. A missing/blank symbol must
        # never be assumed to be NIFTY: doing that can feed a foreign contract
        # into the NIFTY candle and, more importantly, makes the two chart data
        # streams impossible to guarantee as independent.
        symbol = str(symbol).strip()
        if symbol == self.signal_symbol:
            self.last_tick = {"symbol": self.signal_symbol, "ltp": ltp, "time": now}

            # First update the current NIFTY candle, then evaluate the same
            # partial candle for VWAP cross + confirmation.  This is critical
            # for an intrabar/session-first-candle move that crosses VWAP and
            # reaches the confirmation level before the candle closes.
            # Detect a candle rollover before replacing the current candle.
            # Closed-candle VWAP/cross processing runs once per completed candle,
            # not once per FYERS tick. Raw confirmation remains tick-driven.
            previous_candle = self._current_candle
            previous_start = (
                previous_candle.get("datetime")
                if isinstance(previous_candle, dict) else None
            )
            current_candle = self._process_tick_into_candle(message)
            current_start = (
                current_candle.get("datetime")
                if isinstance(current_candle, dict) else None
            )
            candle_rolled = (
                previous_candle is not None
                and previous_start is not None
                and current_start is not None
                and current_start > previous_start
            )
            signal = None

            if current_candle is not None and not self.strategy.trade_active:
                # Latency-critical path: if a setup is armed, evaluate every raw
                # LTP immediately. This preserves instant +15/-15 confirmation.
                signal = self.strategy.process_live_tick(ltp, timestamp=now)

                # Cross detection is a closed-candle operation. Rebuild the
                # prepared history/VWAP row only after the candle rolls over.
                # The previous implementation did this pandas concat + prepare
                # on every tick when no setup was armed.
                if signal is None and candle_rolled and previous_candle is not None:
                    live_df = pd.concat(
                        [self.history_df, pd.DataFrame([previous_candle])],
                        ignore_index=True,
                    )
                    prepared_live = VwapConfirmationEngine.prepare(live_df)
                    live_row = prepared_live.iloc[-1]
                    signal = self.strategy.process_live_candle(live_row)

            if signal:
                if _within_entry_window(now, self.session_start_minute, self.session_end_minute):
                    self.last_signal = signal
                    self.signal_count += 1
                    self.events.put(("signal", signal))
                    self._log(
                        f"ENTRY TRIGGERED BY LIVE CANDLE • BUY {'CE' if signal['side'] == 'BUY' else 'PE'} • "
                        f"triggered at {ltp:.2f} • required {signal['confirmation_level']:.2f}",
                        "entry",
                    )
                    trigger_event = self._record_trigger_event(signal)
                    self.execute_signal(signal, trigger_event=trigger_event)
                    # Allow the next independent VWAP setup to be confirmed.
                    # The strategy flag only prevents duplicate ticks for the
                    # setup that just fired; it is not the position manager.
                    self.strategy.reset_trade()
                else:
                    self.strategy.reset_trade()
                    try:
                        self.strategy._clear_setup()
                    except Exception:
                        pass
                    self._log("ENTRY BLOCKED • outside configured algo entry window (IST).", "info")
        elif self.selected_option and symbol == str(self.selected_option.get("symbol") or ""):
            # Keep the option feed completely isolated from the NIFTY feed.
            # The shared FYERS socket can occasionally deliver an unexpected
            # price payload during reconnect/subscription churn. If the selected
            # option has a known premium reference and the tick is wildly outside
            # a broad contract-local envelope, quarantine it from the premium
            # candle. This prevents a foreign/NIFTY price from becoming a giant
            # wick while still allowing very large legitimate option moves.
            ceiling = self._option_price_ceiling
            if ceiling is not None and ltp > ceiling:
                self._emit_error_once(
                    f"QUARANTINED OPTION TICK • {symbol} @ {ltp:.2f} (ceiling {ceiling:.2f})",
                    key=("option-price-guard", symbol),
                    cooldown=30,
                )
                return
            self.last_execution_tick = {
                "symbol": symbol, "ltp": ltp, "time": now
            }
            self._process_execution_tick(message, now)
        else:
            # Unknown symbols are ignored. They must never mutate either chart.
            return

    def _process_execution_tick(self, message, now=None):
        """Update the selected-option candle from every option tick.

        The option chart used to load history once after entry and then only
        display the latest LTP line. That made the premium candles appear
        frozen. Build/advance a real OHLC candle from the option feed, exactly
        like the NIFTY live candle, and merge it into execution_history.
        """
        if not self.selected_option:
            return

        ltp = message.get("ltp")
        if ltp is None:
            return
        ltp = float(ltp)

        ts_epoch = message.get("last_traded_time") or message.get("timestamp")
        try:
            ts = (
                datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc).astimezone(IST)
                if ts_epoch is not None else (now or datetime.now(IST))
            )
        except (TypeError, ValueError, OverflowError):
            ts = now or datetime.now(IST)

        start = self._candle_start(ts)

        # Start from the latest historical candle when the first live option
        # tick lands inside that same broker candle. This avoids a duplicate
        # current candle and lets the live tick replace its close/high/low.
        if self._execution_current_candle is None:
            hist = self.execution_history
            if hist is not None and not hist.empty and "datetime" in hist.columns:
                try:
                    h = hist.copy()
                    h["datetime"] = pd.to_datetime(h["datetime"])
                    last_dt = h.iloc[-1]["datetime"]
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.tz_localize(IST)
                    else:
                        last_dt = last_dt.tz_convert(IST)
                    if last_dt == start:
                        self._execution_current_candle = {
                            "datetime": start,
                            "open": float(h.iloc[-1]["open"]),
                            "high": float(h.iloc[-1]["high"]),
                            "low": float(h.iloc[-1]["low"]),
                            "close": ltp,
                            "volume": float(h.iloc[-1].get("volume", 0) or 0),
                        }
                    else:
                        self._execution_current_candle = {
                            "datetime": start, "open": ltp, "high": ltp,
                            "low": ltp, "close": ltp, "volume": 0.0
                        }
                except Exception:
                    self._execution_current_candle = {
                        "datetime": start, "open": ltp, "high": ltp,
                        "low": ltp, "close": ltp, "volume": 0.0
                    }
            else:
                self._execution_current_candle = {
                    "datetime": start, "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp, "volume": 0.0
                }

        current = self._execution_current_candle
        if start > current["datetime"]:
            # Commit the finished live candle, replacing any same-timestamp
            # historical row rather than duplicating it.
            base = self.execution_history.copy() if self.execution_history is not None else pd.DataFrame()
            if not base.empty:
                base["datetime"] = pd.to_datetime(base["datetime"])
                if base["datetime"].dt.tz is None:
                    base["datetime"] = base["datetime"].dt.tz_localize(IST)
                else:
                    base["datetime"] = base["datetime"].dt.tz_convert(IST)
                base = base[base["datetime"] != current["datetime"]]
            self.execution_history = pd.concat(
                [base, pd.DataFrame([current])], ignore_index=True
            ).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)

            self._execution_current_candle = {
                "datetime": start, "open": ltp, "high": ltp,
                "low": ltp, "close": ltp, "volume": 0.0
            }
        elif start == current["datetime"]:
            current["high"] = max(float(current["high"]), ltp)
            current["low"] = min(float(current["low"]), ltp)
            current["close"] = ltp

        # Keep the current candle visible immediately. Do not wait for the
        # timeframe to close before the premium chart moves.
        base = self.execution_history.copy() if self.execution_history is not None else pd.DataFrame()
        if not base.empty:
            base["datetime"] = pd.to_datetime(base["datetime"])
            if base["datetime"].dt.tz is None:
                base["datetime"] = base["datetime"].dt.tz_localize(IST)
            else:
                base["datetime"] = base["datetime"].dt.tz_convert(IST)
            base = base[base["datetime"] != current["datetime"]]
        self.execution_history = pd.concat(
            [base, pd.DataFrame([current])], ignore_index=True
        ).drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)

    def display_execution_history(self):
        """Return the selected-option history plus its current live candle."""
        df = self.execution_history.copy() if self.execution_history is not None else pd.DataFrame()
        c = self._execution_current_candle
        if c:
            if not df.empty:
                d = pd.to_datetime(df["datetime"])
                target = pd.Timestamp(c["datetime"])
                if d.dt.tz is None:
                    d = d.dt.tz_localize(IST)
                else:
                    d = d.dt.tz_convert(IST)
                df = df[d != target]
            df = pd.concat([df, pd.DataFrame([c])], ignore_index=True)
            df = df.drop_duplicates("datetime", keep="last").sort_values("datetime").reset_index(drop=True)
        return df

    def _execution_worker(self):
        """Process queued entries independently of the FYERS market-data callback."""
        while not self.execution_stop.is_set():
            try:
                signal, trigger_event = self.execution_queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                if not self.running:
                    self._update_trigger_event(
                        trigger_event, status="CANCELLED", rejection="Engine stopped before execution."
                    )
                else:
                    self._execute_signal_sync(signal, trigger_event=trigger_event)
            except Exception as exc:
                self._update_trigger_event(trigger_event, status="FAILED", rejection=str(exc))
                self._record_rejection(f"Queued execution failed: {exc}")
            finally:
                self.execution_queue.task_done()

    def _start_execution_worker(self):
        if self.execution_thread and self.execution_thread.is_alive():
            return
        self.execution_stop.clear()
        self.execution_thread = threading.Thread(
            target=self._execution_worker,
            name="fyers-vwap-order-worker",
            daemon=True,
        )
        self.execution_thread.start()

    def select_option(self, side):
        chosen = self.client.choose_option(
            self.option_cfg["underlying"], side,
            self.option_cfg["premium_min"], self.option_cfg["premium_max"],
            self.option_cfg["premium_target"], self.option_cfg["expiry_mode"],
            self.option_cfg["strikecount"],
        )
        self.selected_option = chosen
        self.execution_history = pd.DataFrame()
        self._execution_current_candle = None
        try:
            reference = float(chosen.get("ltp"))
        except (TypeError, ValueError):
            reference = None
        self._option_price_reference = reference
        # Very broad guard: this is deliberately far above the selected premium
        # so normal option volatility is untouched, but a NIFTY-scale/foreign
        # price cannot become an option-candle wick. The selected premium band is
        # also considered so a 170-210 contract is not allowed to jump to 1800.
        try:
            configured_max = float(self.option_cfg.get("premium_max"))
        except (TypeError, ValueError):
            configured_max = 0.0
        if reference is not None and reference > 0:
            self._option_price_ceiling = max(reference * 8.0, configured_max * 6.0, reference + 500.0)
        else:
            self._option_price_ceiling = None
        self.events.put(("option", chosen))
        return chosen

    def _protection_values(self, signal):
        mode = self.protection_cfg["mode"]
        if mode == "Points":
            sl = float(self.protection_cfg["sl_points"])
            tgt = float(self.protection_cfg["target_points"])
        elif mode == "Percent":
            sl = float(signal["entry"]) * float(self.protection_cfg["sl_percent"]) / 100.0
            tgt = float(signal["entry"]) * float(self.protection_cfg["target_percent"]) / 100.0
        else:
            atr = float(self.history_df.iloc[-1].get("atr", 0) or 0)
            sl = atr * float(self.protection_cfg["sl_atr_mult"])
            tgt = atr * float(self.protection_cfg["target_atr_mult"])
        return sl, tgt

    def execute_signal(self, signal, trigger_event=None):
        """Queue option selection/order work so the market-data thread never blocks."""
        self.entry_attempts += 1
        self._update_trigger_event(
            trigger_event,
            status="QUEUED",
            queued_time=datetime.now(IST),
        )
        self.execution_queue.put((dict(signal), trigger_event))
        self.events.put(("execution_queued", {
            "event_id": trigger_event.get("event_id") if isinstance(trigger_event, dict) else None,
            "time": datetime.now(IST),
        }))
        self._log(
            f"ENTRY QUEUED • BUY {'CE' if signal.get('side') == 'BUY' else 'PE'} "
            f"• signal={signal.get('side')} • confirmation {float(signal.get('entry', 0)):.2f}",
            "entry",
        )

    def _execute_signal_sync(self, signal, trigger_event=None):
        """Run the same option/protection decision path used by live trading.

        In TEST LIVE ENTRY mode the final broker call is replaced by
        client.place_order(..., dry_run=True), so the exact order payload is
        built and logged without sending anything to FYERS.
        """
        try:
            self._log(
                f"ENTRY ENGINE • BUY {('CE' if signal['side']=='BUY' else 'PE')} "
                f"in ₹{self.option_cfg['premium_min']:.0f}-₹{self.option_cfg['premium_max']:.0f} band…",
                "entry",
            )
            option = self.select_option(signal["side"])
            self._update_trigger_event(
                trigger_event,
                symbol=option.get("symbol", ""),
                option_type=option.get("option_type") or ("CE" if signal["side"] == "BUY" else "PE"),
                strike=option.get("strike"),
                entry=float(option.get("ltp", signal.get("entry", 0.0))),
                status="OPTION_SELECTED",
            )
            self._log(
                f"OPTION SELECTED • {option['symbol']} • {option['option_type']} "
                f"{option['strike']} • LTP ₹{float(option['ltp']):.2f}",
                "option",
            )

            # The strategy direction determines WHICH option we buy:
            # bullish direction -> BUY CE, bearish direction -> BUY PE.
            # The broker-side entry is ALWAYS a BUY because this strategy is
            # long-options-only. A SELL is only an exit/position-close action,
            # never an entry generated from a VWAP direction signal.
            signal_side = str(signal.get("side", "BUY")).upper()
            option_type = str(option.get("option_type") or ("CE" if signal_side == "BUY" else "PE")).upper()
            entry_side = "BUY"
            sl_points, target_points = self._protection_values(signal)
            direction = 1  # long option premium: SL below entry, target above entry
            self.protection = {
                "side": entry_side,
                "signal_side": signal_side,
                "option_type": option_type,
                "entry_reference": option["ltp"],
                "sl_points": sl_points,
                "target_points": target_points,
                "sl_price": option["ltp"] - direction * sl_points,
                "target_price": option["ltp"] + direction * target_points,
                "enabled": bool(self.protection_cfg.get("enabled", True)),
            }

            product = "BO" if self.protection_cfg.get("enabled", True) else "INTRADAY"
            order_kwargs = dict(
                symbol=option["symbol"],
                # NEVER send a SELL as an entry. The VWAP bearish direction
                # has already been converted to BUY PE by select_option().
                side=1,
                qty=self.qty,
                order_type=2,
                product_type=product,
                stop_loss=sl_points if product == "BO" else 0,
                take_profit=target_points if product == "BO" else 0,
                dry_run=not self.live_trading,
                order_tag="VWAPBOT-TEST" if self.test_live_entry else "VWAPBOT",
            )

            if self.live_trading:
                self._log(
                    f"LIVE ORDER CALL • {order_kwargs['symbol']} • "
                    f"BUY {option_type} qty {self.qty} • signal={signal_side} • product {product}",
                    "live",
                )
                result = self.client.place_order(**order_kwargs)
                if isinstance(result, dict) and result.get("s") not in ("ok",):
                    message = result.get("message") or result.get("error") or result.get("code") or str(result)
                    self._record_rejection(f"FYERS rejected order: {message}", result)
                    self.last_order = result
                    self._update_trigger_event(trigger_event, status="REJECTED", rejection=message)
                    self.events.put(("order", result))
                    return
                self._update_trigger_event(trigger_event, status="EXECUTED")
                self._log(
                    f"LIVE ORDER ACCEPTED • broker response {result}",
                    "live",
                )
            elif self.test_live_entry:
                # This is the real execution path in every respect except the
                # network side effect. dry_run=True makes FyersClient build the
                # exact FYERS payload and return it locally.
                result = self.client.place_order(**order_kwargs)
                self.test_entry_count += 1
                self._update_trigger_event(trigger_event, status="TEST")
                self._log(
                    f"TEST LIVE ENTRY • NO ORDER SENT • exact FYERS payload prepared "
                    f"for {option['symbol']} • BUY {option_type} qty {self.qty} • "
                    f"SL {sl_points:.2f} • Target {target_points:.2f}",
                    "test",
                )
            else:
                result = {
                    "s": "paper_signal",
                    "symbol": option["symbol"],
                    "side": "BUY",
                    "signal_side": signal_side,
                    "option_type": option_type,
                    "qty": self.qty,
                    "entry": option["ltp"],
                    "sl_points": sl_points,
                    "target_points": target_points,
                }
                self._update_trigger_event(trigger_event, status="PAPER")
                self._log(
                    f"PAPER ENTRY PATH • {option['symbol']} @ ₹{float(option['ltp']):.2f}",
                    "paper",
                )

            self.last_order = result

            # Record the execution immediately after the entry path succeeds.
            # `chart_time` is the actual timeframe bucket containing the trigger,
            # which is critical for Lightweight Charts markers: marker timestamps
            # must match a candle's timestamp, not an arbitrary intrabar tick time.
            signal_time = signal.get("time") or datetime.now(IST)
            try:
                chart_time = self._candle_start(pd.Timestamp(signal_time))
            except Exception:
                chart_time = pd.Timestamp(signal_time)
            # The trigger event was already recorded before option selection.
            # Keep its exact chart bucket and enrich it with the final contract
            # details instead of creating a second marker on the same candle.
            execution_event = trigger_event or self._record_trigger_event(signal)
            execution_event.update({
                "execution_time": pd.Timestamp.now(tz=IST),
                "entry_time": signal_time,
                "chart_time": chart_time,
                "side": "BUY",
                "signal_side": signal_side,
                "option_type": option_type,
                "symbol": option.get("symbol", ""),
                "strike": option.get("strike"),
                "entry": float(option.get("ltp", signal.get("entry", 0.0))),
                "trigger_price": float(signal.get("entry", 0.0)),
                "quantity": int(self.qty),
                "lots": 1,
                "status": execution_event.get("status") or ("EXECUTED" if self.live_trading else ("TEST" if self.test_live_entry else "PAPER")),
                "expiry": option.get("expiry"),
                "protection_enabled": bool(self.protection_cfg.get("enabled", True)),
                "sl_price": float(self.protection.get("sl_price")) if self.protection else None,
                "target_price": float(self.protection.get("target_price")) if self.protection else None,
            })
            self._update_trigger_event(trigger_event, **execution_event)
            self.events.put(("order", result))

            try:
                self.execution_history = self.client.history(option["symbol"], self.resolution, 31)
            except Exception:
                self.execution_history = pd.DataFrame()

            # Keep the selected option live feed for charting and paper P&L.
            self._subscribe_execution(option["symbol"])
        except Exception as exc:
            self._update_trigger_event(trigger_event, status="FAILED", rejection=str(exc))
            self._record_rejection(f"Entry/protection failed: {exc}")

    def _subscribe_execution(self, symbol):
        # Reuse the existing market-data socket. Keep the symbol in the desired
        # set so FYERS can restore it after a reconnect.
        symbol = str(symbol)
        self.data_symbols.add(symbol)
        try:
            if self.socket is not None and self.ws_connected:
                if self._last_option_subscribe != symbol:
                    self.client.subscribe_data_socket(self.socket, [symbol], "SymbolUpdate")
                    self._last_option_subscribe = symbol
                    self._log(f"LIVE OPTION FEED • subscribed {symbol}", "info")
            else:
                self._log("Option feed queued until the market-data socket reconnects.", "info")
        except Exception as exc:
            self._emit_error_once(
                f"Option subscription failed: {exc}",
                key=("option-sub", symbol, str(exc)),
                cooldown=120,
            )

    def refresh_execution_chart(self):
        if not self.selected_option:
            return self.execution_history
        try:
            df = self.client.history(self.selected_option["symbol"], self.resolution, 31)
            self.execution_history = df
            self._execution_current_candle = None
            return df
        except Exception:
            return self.execution_history

    @property
    def current_candle(self):
        return self._current_candle.copy() if self._current_candle else None

    def display_history(self):
        df = self.history_df.copy()
        c = self.current_candle
        if c:
            live = pd.DataFrame([c])
            live["vwap"] = self.current_vwap
            if not df.empty:
                live["atr"] = df.iloc[-1].get("atr")
            df = pd.concat([df, live], ignore_index=True)
        return df

    def tick_age_seconds(self):
        if not self.last_tick:
            return None
        import time
        if self._last_tick_monotonic:
            return max(0.0, time.monotonic() - self._last_tick_monotonic)
        return max(0.0, (datetime.now(IST) - self.last_tick["time"]).total_seconds())

    def bar_seconds_remaining(self):
        c = self.current_candle
        if not c:
            return None
        end = c["datetime"] + pd.Timedelta(minutes=self._timeframe_minutes())
        return max(0, int((end - datetime.now(IST)).total_seconds()))

    def drain_events(self):
        items=[]
        while True:
            try: items.append(self.events.get_nowait())
            except Empty: break
        return items
