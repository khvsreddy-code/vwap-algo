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
        self.execution_socket = None
        self.execution_thread = None
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
        self.ws_state = "STOPPED"
        self.ws_error = None
        self._ws_error_signature = None
        self._ws_error_time = None
        self._ws_live_since_attempt = False
        self._ws_attempt = 0
        self.entry_attempts = 0
        self.test_entry_count = 0
        self._last_armed_key = None
        self._last_entry_log = None
        self.last_rejection = None

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
        self.current_vwap = None if pd.isna(v) else float(v)
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
        self.load_history()
        self.thread = threading.Thread(target=self._run_socket, daemon=True)
        self.thread.start()
        self.events.put(("status", "Engine started"))

    def stop(self):
        self.running = False
        self.ws_connected = False
        self.ws_state = "STOPPED"
        for sock_name in ("socket", "execution_socket"):
            sock = getattr(self, sock_name, None)
            if sock is not None:
                try:
                    close = getattr(sock, "close", None)
                    if callable(close):
                        close()
                except Exception:
                    pass
        self.events.put(("status", "Stop requested"))

    def _on_ws_connect(self, _sock=None):
        self.ws_connected = True
        self.ws_state = "LIVE"
        self.ws_error = None
        self._ws_live_since_attempt = True
        self._ws_attempt = 0
        self.last_ws_event = datetime.now(IST)
        self.events.put(("status", "LIVE"))

    def _on_ws_close(self, msg=None):
        self.ws_connected = False
        self.last_ws_event = datetime.now(IST)
        self.ws_state = "RECONNECTING" if self.running else "STOPPED"
        self.events.put(("status", "RECONNECTING" if self.running else "DISCONNECTED"))

    def _on_ws_error(self, msg=None):
        self.ws_connected = False
        self.last_ws_event = datetime.now(IST)
        self.ws_state = "RECONNECTING" if self.running else "ERROR"
        text = str(msg or "Unknown WebSocket error")
        self.ws_error = text
        now = datetime.now(IST)
        signature = text[:300]
        # FYERS may emit the same remote-host error several times during
        # recovery; throttle duplicate UI events while keeping the state.
        if (signature != self._ws_error_signature or self._ws_error_time is None
                or (now - self._ws_error_time).total_seconds() >= 5):
            self._ws_error_signature = signature
            self._ws_error_time = now
            self.events.put(("error", f"Market data connection error: {text}"))
            if self.running:
                self.events.put(("status", "RECONNECTING"))

    def _run_socket(self):
        # FYERS SDK reconnects internally; this supervisor also recreates the
        # socket if connect() exits, so remote-host disconnects don't kill the
        # engine or destroy the chart state.
        import time
        backoff = 1.0
        while self.running:
            self._ws_attempt += 1
            self._ws_live_since_attempt = False
            try:
                self.ws_state = "CONNECTING" if not self.ws_connected else "RECONNECTING"
                self.events.put(("status", self.ws_state))
                self.socket = self.client.start_data_socket(
                    [self.signal_symbol],
                    on_message=self._on_tick,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                    on_connect=self._on_ws_connect,
                )
                if self._ws_live_since_attempt:
                    backoff = 1.0
                else:
                    backoff = min(backoff * 2.0, 15.0)
            except Exception as exc:
                self.ws_connected = False
                self.ws_state = "RECONNECTING" if self.running else "STOPPED"
                self.ws_error = str(exc)
                self.events.put(("error", f"Market data socket: {exc}"))
                backoff = min(backoff * 2.0, 15.0)
            if self.running:
                self.events.put(("status", f"RECONNECTING • retry in {backoff:.0f}s"))
                time.sleep(backoff)

    def _on_tick(self, message):
        if not isinstance(message, dict):
            return
        ltp = message.get("ltp")
        if ltp is not None:
            now = datetime.now(IST)
            ltp = float(ltp)
            self.ws_connected = True
            self.last_ws_event = now
            self.last_tick = {"symbol": self.signal_symbol, "ltp": ltp, "time": now}

            # Confirmation is checked on every live tick. This is the same
            # condition used by the real-entry path; test-live mode below only
            # changes whether the broker endpoint is called.
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
        # Run the option websocket off the Streamlit script thread. The previous
        # implementation could block on keep_running(), which made paper buttons
        # appear to do nothing and prevented the option feed from updating.
        def on_message(msg):
            if not isinstance(msg, dict) or msg.get("ltp") is None:
                return
            self.last_execution_tick = {
                "symbol": symbol, "ltp": float(msg["ltp"]), "time": datetime.now(IST)
            }
        def worker():
            try:
                self.execution_socket = self.client.start_data_socket(
                    [symbol], on_message=on_message
                )
            except Exception as exc:
                self.events.put(("error", f"Execution chart feed failed: {exc}"))
        if self.execution_thread and self.execution_thread.is_alive():
            return
        self.execution_thread = threading.Thread(target=worker, daemon=True, name="fyers-option-feed")
        self.execution_thread.start()

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
