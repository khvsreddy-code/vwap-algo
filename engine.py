import threading
from queue import Queue, Empty
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from fyers_client import FyersClient
from strategy import VwapConfirmationEngine, StrategyConfig

IST = ZoneInfo("Asia/Kolkata")


class TradingEngine:
    def __init__(self, client: FyersClient, signal_symbol: str, execution_symbol: str,
                 resolution: str, confirmation_points: float, confirmation_bars: int,
                 qty: int, live_trading: bool):
        self.client = client
        self.signal_symbol = signal_symbol
        self.execution_symbol = execution_symbol or signal_symbol
        self.resolution = resolution
        self.qty = qty
        self.live_trading = live_trading
        self.strategy = VwapConfirmationEngine(StrategyConfig(confirmation_points, confirmation_bars))
        self.events = Queue()
        self.running = False
        self.thread = None
        self.socket = None
        self.history_df = pd.DataFrame()
        self.last_tick = {}
        self.last_signal = None
        self.last_order = None
        self.current_vwap = None
        self.signal_count = 0
        self._current_candle = None
        self._last_cum_volume = None

    def load_history(self, days=10):
        df = self.client.history(self.signal_symbol, self.resolution, days)
        if not df.empty:
            # Only closed candles should feed the strategy. The latest candle may still be open.
            df = df.iloc[:-1].copy() if len(df) > 1 else df
            df = self._prepare(df)
        self.history_df = df
        return df

    @staticmethod
    def _prepare(df):
        out = df.copy()
        out["ohlc4"] = (out["open"] + out["high"] + out["low"] + out["close"]) / 4.0
        out["date"] = out["datetime"].dt.date
        out["pv"] = out["ohlc4"] * out["volume"]
        out["cum_pv"] = out.groupby("date")["pv"].cumsum()
        out["cum_vol"] = out.groupby("date")["volume"].cumsum()
        out["vwap"] = out["cum_pv"] / out["cum_vol"].replace(0, pd.NA)
        out["prev_ohlc4"] = out["ohlc4"].shift(1)
        out["prev_vwap"] = out["vwap"].shift(1)
        return out


    def _timeframe_minutes(self):
        if self.resolution.endswith("S"):
            return max(1, int(self.resolution[:-1]) // 60)
        return int(self.resolution)

    def _candle_start(self, ts):
        ts = ts.astimezone(IST)
        minutes = self._timeframe_minutes()
        total = ts.hour * 60 + ts.minute
        bucket = (total // minutes) * minutes
        return ts.replace(hour=bucket // 60, minute=bucket % 60, second=0, microsecond=0)

    def _process_tick_into_candle(self, message):
        ts_epoch = message.get("last_traded_time") or message.get("timestamp")
        if ts_epoch is None:
            ts = datetime.now(IST)
        else:
            ts = datetime.fromtimestamp(float(ts_epoch), tz=timezone.utc).astimezone(IST)
        ltp = message.get("ltp")
        if ltp is None:
            return
        ltp = float(ltp)
        start = self._candle_start(ts)

        # FYERS symbol updates expose cumulative traded volume when available.
        cum_vol = message.get("vol_traded_today")
        if cum_vol is not None:
            try:
                cum_vol = float(cum_vol)
            except (TypeError, ValueError):
                cum_vol = None
        if cum_vol is not None and self._last_cum_volume is not None:
            tick_volume = max(0.0, cum_vol - self._last_cum_volume)
        elif cum_vol is not None:
            tick_volume = 0.0
        else:
            tick_volume = 0.0
        if cum_vol is not None:
            self._last_cum_volume = cum_vol

        if self._current_candle is None:
            self._current_candle = {"datetime": start, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": tick_volume}
            return

        if start > self._current_candle["datetime"]:
            closed = pd.Series(self._current_candle)
            self._accept_closed_live_candle(closed)
            self._current_candle = {"datetime": start, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": tick_volume}
        elif start == self._current_candle["datetime"]:
            self._current_candle["high"] = max(self._current_candle["high"], ltp)
            self._current_candle["low"] = min(self._current_candle["low"], ltp)
            self._current_candle["close"] = ltp
            self._current_candle["volume"] += tick_volume

    def _accept_closed_live_candle(self, candle):
        # Append the closed candle and calculate session VWAP using all known candles.
        base = pd.DataFrame([candle])
        self.history_df = pd.concat([self.history_df, base], ignore_index=True)
        self.history_df = self.history_df.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime").reset_index(drop=True)
        prepared = self._prepare(self.history_df)
        row = prepared.iloc[-1]
        signal = self.strategy.process_closed_candle(row)
        self.history_df = prepared
        self.current_vwap = None if pd.isna(row.get("vwap")) else float(row.get("vwap"))
        self.events.put(("candle", row.to_dict()))
        if signal:
            self.last_signal = {"time": row["datetime"], **signal}
            self.signal_count += 1
            self.events.put(("signal", self.last_signal))
            self.submit_signal(signal)

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
        # FYERS socket runs its own loop; reconnecting is handled by SDK.
        # The daemon thread will terminate with the Streamlit process.

    def _run_socket(self):
        try:
            self.socket = self.client.start_data_socket(
                [self.signal_symbol],
                on_message=self._on_tick,
                on_error=lambda msg: self.events.put(("error", str(msg))),
                on_close=lambda msg: self.events.put(("status", f"Socket closed: {msg}")),
            )
        except Exception as exc:
            self.events.put(("error", repr(exc)))
            self.running = False

    def _on_tick(self, message):
        if not isinstance(message, dict):
            return
        symbol = message.get("symbol") or self.signal_symbol
        ltp = message.get("ltp")
        if ltp is not None:
            self.last_tick = {"symbol": symbol, "ltp": float(ltp), "time": datetime.now(IST)}
            self.events.put(("tick", self.last_tick.copy()))
            self._process_tick_into_candle(message)

    def process_history_signal_test(self):
        """Replay closed history through the strategy; useful for validating Pine parity."""
        if self.history_df.empty:
            return []
        signals = []
        self.strategy = VwapConfirmationEngine(self.strategy.config)
        for _, row in self.history_df.iterrows():
            signal = self.strategy.process_closed_candle(row)
            if signal:
                signals.append({"time": row["datetime"], **signal})
        return signals

    def submit_signal(self, signal):
        side = 1 if signal["side"] == "BUY" else -1
        result = self.client.place_market_order(
            self.execution_symbol, side, self.qty, dry_run=not self.live_trading
        )
        self.last_order = result
        self.events.put(("order", result))

    def drain_events(self):
        items = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except Empty:
                break
        return items
