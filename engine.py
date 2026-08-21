import threading
from queue import Queue, Empty
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from fyers_client import FyersClient
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
        side = "BUY" if s.cross_direction == 1 else "SELL"
        self._log(
            f"ENTRY ARMED [{source.upper()}] • {side} • VWAP cross close "
            f"{s.cross_price:.2f} • trigger {s.confirmation_level:.2f} "
            f"• window {s.config.confirmation_bars} candles",
            "arm",
        )

    def load_history(self, days=10):
        df = self.client.history(self.signal_symbol, self.resolution, days)
        if len(df) > 1:
            df = df.iloc[:-1].copy()
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
                    f"ENTRY TRIGGERED • {signal['side']} • price {signal['entry']:.2f} "
                    f"• VWAP cross close {signal['cross_price']:.2f} • "
                    f"bar {signal['bars_since_cross']}",
                    "entry",
                )
                self.execute_signal(signal)
            else:
                self.strategy.reset_trade()
                try:
                    self.strategy._clear_setup()
                except Exception:
                    pass
                self._log("ENTRY BLOCKED • outside configured algo entry window (IST).", "info")

    def start(self):
        if self.running:
            return
        self.running = True
        if self.history_df.empty:
            self.load_history()
        self.thread = threading.Thread(target=self._run_socket, daemon=True)
        self.thread.start()
        self.events.put(("status", "Engine started"))

    def stop(self):
        self.running = False
        sock = self.socket
        self.socket = None
        try:
            if sock is not None and hasattr(sock, "close_connection"):
                sock.close_connection()
            elif sock is not None and hasattr(sock, "close"):
                sock.close()
        except Exception:
            pass
        self.ws_connected = False
        self.market_data_reconnecting = False
        self.events.put(("status", "STOPPED"))

    def _on_ws_connect(self, sock=None):
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

    def _run_socket(self):
        # One socket per engine. FYERS owns reconnects internally. If the SDK
        # itself returns from connect(), wait once and retry slowly rather than
        # spinning a reconnect loop.
        import time
        self._socket_started_at = datetime.now(IST)
        try:
            self.socket = self.client.start_data_socket(
                sorted(self.data_symbols),
                on_message=self._on_tick,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
                on_connect=self._on_ws_connect,
                lite_mode=self.market_data_lite,
                queue_interval_ms=50,
            )
        except Exception as exc:
            self.ws_connected = False
            self.market_data_reconnecting = True
            self._emit_error_once(
                f"Market data socket failed: {exc}",
                key=("socket", str(exc)),
                cooldown=60,
            )
        finally:
            self.socket = None
            if self.running and not self.market_data_blocked:
                self.market_data_reconnecting = True
                # Do not hammer FYERS if its socket exits unexpectedly.
                time.sleep(15)

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

        # The same socket carries both NIFTY and the selected option. Never let
        # an option tick overwrite the NIFTY strategy/chart tick.
        if symbol == self.signal_symbol or not symbol:
            self.last_tick = {"symbol": self.signal_symbol, "ltp": ltp, "time": now}

            # First update the current NIFTY candle, then evaluate the same
            # partial candle for VWAP cross + confirmation.  This is critical
            # for an intrabar/session-first-candle move that crosses VWAP and
            # reaches the confirmation level before the candle closes.
            current_candle = self._process_tick_into_candle(message)
            signal = None
            if current_candle is not None and not self.strategy.trade_active:
                live_df = pd.concat(
                    [self.history_df, pd.DataFrame([current_candle])],
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
                        f"ENTRY TRIGGERED BY LIVE CANDLE • {signal['side']} • "
                        f"triggered at {ltp:.2f} • required {signal['confirmation_level']:.2f}",
                        "entry",
                    )
                    self.execute_signal(signal)
                else:
                    self.strategy.reset_trade()
                    try:
                        self.strategy._clear_setup()
                    except Exception:
                        pass
                    self._log("ENTRY BLOCKED • outside configured algo entry window (IST).", "info")
        elif self.selected_option and symbol == self.selected_option.get("symbol"):
            self.last_execution_tick = {
                "symbol": symbol, "ltp": ltp, "time": now
            }
            self._process_execution_tick(message, now)

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

    def execute_signal(self, signal):
        """Run the same option/protection decision path used by live trading.

        In TEST LIVE ENTRY mode the final broker call is replaced by
        client.place_order(..., dry_run=True), so the exact order payload is
        built and logged without sending anything to FYERS.
        """
        self.entry_attempts += 1
        try:
            self._log(
                f"ENTRY ENGINE • selecting {('CE' if signal['side']=='BUY' else 'PE')} "
                f"in ₹{self.option_cfg['premium_min']:.0f}-₹{self.option_cfg['premium_max']:.0f} band…",
                "entry",
            )
            option = self.select_option(signal["side"])
            self._log(
                f"OPTION SELECTED • {option['symbol']} • {option['option_type']} "
                f"{option['strike']} • LTP ₹{float(option['ltp']):.2f}",
                "option",
            )

            sl_points, target_points = self._protection_values(signal)
            direction = 1 if signal["side"] == "BUY" else -1
            self.protection = {
                "side": signal["side"],
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
                side=1 if signal["side"] == "BUY" else -1,
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
                    f"{signal['side']} qty {self.qty} • product {product}",
                    "live",
                )
                result = self.client.place_order(**order_kwargs)
                if isinstance(result, dict) and result.get("s") not in ("ok",):
                    message = result.get("message") or result.get("error") or result.get("code") or str(result)
                    self._record_rejection(f"FYERS rejected order: {message}", result)
                    self.last_order = result
                    self.events.put(("order", result))
                    return
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
                self._log(
                    f"TEST LIVE ENTRY • NO ORDER SENT • exact FYERS payload prepared "
                    f"for {option['symbol']} • {signal['side']} qty {self.qty} • "
                    f"SL {sl_points:.2f} • Target {target_points:.2f}",
                    "test",
                )
            else:
                result = {
                    "s": "paper_signal",
                    "symbol": option["symbol"],
                    "side": signal["side"],
                    "qty": self.qty,
                    "entry": option["ltp"],
                    "sl_points": sl_points,
                    "target_points": target_points,
                }
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
            execution_event = {
                "execution_time": pd.Timestamp.now(tz=IST),
                "entry_time": signal_time,
                "chart_time": chart_time,
                "side": signal["side"],
                "option_type": option.get("option_type") or ("CE" if signal["side"] == "BUY" else "PE"),
                "symbol": option.get("symbol", ""),
                "strike": option.get("strike"),
                "entry": float(option.get("ltp", signal.get("entry", 0.0))),
                "trigger_price": float(signal.get("entry", 0.0)),
                "quantity": int(self.qty),
                "lots": 1,
                "status": "EXECUTED" if self.live_trading else ("TEST" if self.test_live_entry else "PAPER"),
            }
            self.execution_events.append(execution_event)
            self.execution_events = self.execution_events[-100:]
            self.events.put(("execution", execution_event))
            self.events.put(("order", result))

            try:
                self.execution_history = self.client.history(option["symbol"], self.resolution, 3)
            except Exception:
                self.execution_history = pd.DataFrame()

            # Keep the selected option live feed for charting and paper P&L.
            self._subscribe_execution(option["symbol"])
        except Exception as exc:
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
            df = self.client.history(self.selected_option["symbol"], self.resolution, 3)
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
