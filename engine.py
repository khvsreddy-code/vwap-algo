import threading
from queue import Queue, Empty
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from fyers_client import FyersClient
from strategy import VwapConfirmationEngine, StrategyConfig

IST = ZoneInfo("Asia/Kolkata")


class TradingEngine:
    def __init__(self, client, signal_symbol, resolution, confirmation_points, confirmation_bars,
                 qty, live_trading, option_cfg, protection_cfg, test_live_entry=False):
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
        self.ws_connected = False
        self.last_ws_event = None
        self.entry_attempts = 0
        self.test_entry_count = 0
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
            # Important: rebuild the pending VWAP setup from closed history.
            # Without this, a cross in the last historical candle is invisible
            # to the live engine until another fresh cross occurs.
            self.strategy.seed_from_history(self.history_df)
            self._log_setup_state("history")
        return self.history_df

    def _timeframe_minutes(self):
        return max(1, int(self.resolution))

    def _candle_start(self, ts):
        ts = ts.astimezone(IST)
        minutes = self._timeframe_minutes()
        total = ts.hour * 60 + ts.minute
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
            self._current_candle = {"datetime":start,"open":ltp,"high":ltp,"low":ltp,"close":ltp,"volume":tick_volume}
            return
        if start > self._current_candle["datetime"]:
            self._accept_closed_live_candle(pd.Series(self._current_candle))
            self._current_candle = {"datetime":start,"open":ltp,"high":ltp,"low":ltp,"close":ltp,"volume":tick_volume}
        elif start == self._current_candle["datetime"]:
            self._current_candle["high"] = max(self._current_candle["high"], ltp)
            self._current_candle["low"] = min(self._current_candle["low"], ltp)
            self._current_candle["close"] = ltp
            self._current_candle["volume"] += tick_volume

    def _accept_closed_live_candle(self, candle):
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

            signal = self.strategy.process_live_tick(ltp, now)
            if signal:
                self.last_signal = signal
                self.signal_count += 1
                self.events.put(("signal", signal))
                self._log(
                    f"ENTRY TRIGGERED BY LIVE TICK • {signal['side']} • "
                    f"triggered at {ltp:.2f} • required {signal['confirmation_level']:.2f}",
                    "entry",
                )
                self.execute_signal(signal)

            self._process_tick_into_candle(message)
        elif self.selected_option and symbol == self.selected_option.get("symbol"):
            self.last_execution_tick = {
                "symbol": symbol, "ltp": ltp, "time": now
            }

    def select_option(self, side):
        chosen = self.client.choose_option(
            self.option_cfg["underlying"], side,
            self.option_cfg["premium_min"], self.option_cfg["premium_max"],
            self.option_cfg["premium_target"], self.option_cfg["expiry_mode"],
            self.option_cfg["strikecount"],
        )
        self.selected_option = chosen
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
