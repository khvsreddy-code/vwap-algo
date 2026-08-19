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
                 qty, live_trading, option_cfg, protection_cfg):
        self.client = client
        self.signal_symbol = signal_symbol
        self.resolution = resolution
        self.qty = int(qty)
        self.live_trading = live_trading
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

    def load_history(self, days=10):
        df = self.client.history(self.signal_symbol, self.resolution, days)
        if len(df) > 1:
            df = df.iloc[:-1].copy()
        self.history_df = VwapConfirmationEngine.prepare(df) if not df.empty else df
        if not self.history_df.empty:
            v = self.history_df.iloc[-1].get("vwap")
            self.current_vwap = None if pd.isna(v) else float(v)
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
        signal = self.strategy.process_closed_candle(row)
        if signal:
            self.last_signal = signal
            self.signal_count += 1
            self.events.put(("signal", signal))
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
        self.events.put(("status", "Stop requested"))

    def _on_ws_connect(self, _sock=None):
        self.ws_connected = True
        self.last_ws_event = datetime.now(IST)
        self.events.put(("status", "LIVE"))

    def _on_ws_close(self, msg=None):
        self.ws_connected = False
        self.last_ws_event = datetime.now(IST)
        self.events.put(("status", "DISCONNECTED"))

    def _on_ws_error(self, msg=None):
        self.ws_connected = False
        self.last_ws_event = datetime.now(IST)
        self.events.put(("error", f"Market data connection error: {msg}"))

    def _run_socket(self):
        try:
            self.socket = self.client.start_data_socket(
                [self.signal_symbol], on_message=self._on_tick,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
                on_connect=self._on_ws_connect,
            )
        except Exception as exc:
            self.events.put(("error", repr(exc)))
            self.running = False

    def _on_tick(self, message):
        if not isinstance(message, dict):
            return
        ltp = message.get("ltp")
        if ltp is not None:
            now = datetime.now(IST)
            self.ws_connected = True
            self.last_ws_event = now
            self.last_tick = {"symbol": self.signal_symbol, "ltp": float(ltp), "time": now}
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
        try:
            option = self.select_option(signal["side"])
            sl_points, target_points = self._protection_values(signal)
            self.protection = {"entry_reference": option["ltp"], "sl_points":sl_points,
                               "target_points":target_points,
                               "sl_price":option["ltp"]-sl_points,
                               "target_price":option["ltp"]+target_points,
                               "enabled": bool(self.protection_cfg.get("enabled", True))}
            product = "BO" if self.protection_cfg.get("enabled", True) else "INTRADAY"
            result = self.client.place_order(
                option["symbol"], side=1, qty=self.qty, order_type=2,
                product_type=product,
                stop_loss=sl_points if product == "BO" else 0,
                take_profit=target_points if product == "BO" else 0,
                dry_run=not self.live_trading, order_tag="VWAPBOT",
            )
            self.last_order = result
            self.events.put(("order", result))
            try:
                self.execution_history = self.client.history(option["symbol"], self.resolution, 3)
            except Exception:
                self.execution_history = pd.DataFrame()
            if self.live_trading:
                self._subscribe_execution(option["symbol"])
        except Exception as exc:
            self.events.put(("error", f"Entry/protection failed: {exc}"))

    def _subscribe_execution(self, symbol):
        # A second socket keeps the execution option chart live after selection.
        def on_message(msg):
            if not isinstance(msg, dict) or msg.get("ltp") is None:
                return
            self.last_execution_tick = {"symbol":symbol, "ltp":float(msg["ltp"]), "time":datetime.now(IST)}
        try:
            self.execution_socket = self.client.start_data_socket([symbol], on_message=on_message)
            self.execution_history = self.client.history(symbol, self.resolution, 3)
        except Exception as exc:
            self.events.put(("error", f"Execution chart feed failed: {exc}"))

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
