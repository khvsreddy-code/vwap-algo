from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class StrategyConfig:
    confirmation_points: float = 15.0
    confirmation_bars: int = 5


class VwapConfirmationEngine:
    """
    VWAP confirmation state machine.

    Rule:
      1. A COMPLETED candle must close across VWAP.
      2. No entry is taken on that crossing candle.
      3. During the next N candles, a BUY setup triggers when price reaches
         cross-close + confirmation_points; a SELL setup triggers at
         cross-close - confirmation_points.
      4. Live ticks can trigger the entry intrabar; historical candle high/low
         is also checked so a move is not missed.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.cross_price: Optional[float] = None
        self.cross_bar: Optional[int] = None
        self.cross_direction = 0
        self.confirmation_level: Optional[float] = None
        self.bar_index = -1
        self.trade_active = False
        self.trade_direction = 0
        self.last_signal = None

    @property
    def bars_since_cross(self):
        if self.cross_bar is None:
            return None
        return self.bar_index - self.cross_bar

    @property
    def armed(self):
        return self.cross_bar is not None and self.cross_direction != 0 and not self.trade_active

    def _arm(self, direction, close):
        self.cross_price = float(close)
        self.cross_bar = self.bar_index
        self.cross_direction = int(direction)
        self.confirmation_level = (
            self.cross_price + self.config.confirmation_points
            if direction == 1
            else self.cross_price - self.config.confirmation_points
        )

    def _clear_setup(self):
        self.cross_price = None
        self.cross_bar = None
        self.cross_direction = 0
        self.confirmation_level = None

    def _make_signal(self, side, entry, row=None, bars=None):
        self.trade_active = True
        self.trade_direction = 1 if side == "BUY" else -1
        signal = {
            "side": side,
            "entry": float(entry),
            "cross_price": float(self.cross_price),
            "confirmation_level": float(self.confirmation_level),
            "bars_since_cross": int(bars if bars is not None else max(0, self.bar_index - self.cross_bar)),
            "time": row["datetime"] if row is not None else pd.Timestamp.now(tz="Asia/Kolkata"),
        }
        self.last_signal = signal
        self._clear_setup()
        return signal

    def _maybe_expire(self, current_bar):
        if self.cross_bar is not None and current_bar - self.cross_bar > self.config.confirmation_bars:
            self._clear_setup()

    def seed_from_history(self, df: pd.DataFrame):
        """
        Rebuild only the setup state from already-closed history.
        It NEVER emits an order/signal. This prevents the live engine from
        missing a cross that happened in the last completed historical candle.
        """
        self.cross_price = None
        self.cross_bar = None
        self.cross_direction = 0
        self.confirmation_level = None
        self.trade_active = False
        self.trade_direction = 0
        self.last_signal = None
        self.bar_index = -1

        if df is None or df.empty:
            return

        for _, row in df.reset_index(drop=True).iterrows():
            self.bar_index += 1
            close = float(row["close"])
            vwap = row.get("vwap")
            prev_close = row.get("prev_close")
            prev_vwap = row.get("prev_vwap")
            if pd.isna(vwap) or pd.isna(prev_close) or pd.isna(prev_vwap):
                continue

            long_cross = float(prev_close) <= float(prev_vwap) and close > float(vwap)
            short_cross = float(prev_close) >= float(prev_vwap) and close < float(vwap)
            if long_cross:
                self._arm(1, close)
            elif short_cross:
                self._arm(-1, close)
            self._maybe_expire(self.bar_index)

    def process_closed_candle(self, row: pd.Series):
        self.bar_index += 1
        close = float(row["close"])
        high = float(row.get("high", close))
        low = float(row.get("low", close))
        vwap = row.get("vwap")
        prev_close = row.get("prev_close")
        prev_vwap = row.get("prev_vwap")

        if pd.isna(vwap) or pd.isna(prev_close) or pd.isna(prev_vwap):
            self._maybe_expire(self.bar_index)
            return None

        long_cross = float(prev_close) <= float(prev_vwap) and close > float(vwap)
        short_cross = float(prev_close) >= float(prev_vwap) and close < float(vwap)

        # A fresh cross replaces any older pending setup.
        if long_cross:
            self._arm(1, close)
            return None
        if short_cross:
            self._arm(-1, close)
            return None

        if self.cross_bar is None or self.trade_active:
            return None

        bars = self.bar_index - self.cross_bar
        if 1 <= bars <= self.config.confirmation_bars:
            if self.cross_direction == 1 and high >= self.confirmation_level:
                return self._make_signal("BUY", self.confirmation_level, row, bars)
            if self.cross_direction == -1 and low <= self.confirmation_level:
                return self._make_signal("SELL", self.confirmation_level, row, bars)

        self._maybe_expire(self.bar_index)
        return None

    def process_live_tick(self, ltp, timestamp=None):
        """
        Trigger the pending confirmation as soon as a live tick reaches the
        required level in a later candle. The crossing candle itself is never
        used for the confirmation.
        """
        if self.cross_bar is None or self.trade_active:
            return None

        current_bar = self.bar_index + 1
        bars = current_bar - self.cross_bar
        if bars < 1:
            return None
        if bars > self.config.confirmation_bars:
            self._clear_setup()
            return None

        ltp = float(ltp)
        if self.cross_direction == 1 and ltp >= float(self.confirmation_level):
            return self._make_signal(
                "BUY", ltp, {"datetime": timestamp or pd.Timestamp.now(tz="Asia/Kolkata")}, bars
            )
        if self.cross_direction == -1 and ltp <= float(self.confirmation_level):
            return self._make_signal(
                "SELL", ltp, {"datetime": timestamp or pd.Timestamp.now(tz="Asia/Kolkata")}, bars
            )
        return None

    def reset_trade(self):
        self.trade_active = False
        self.trade_direction = 0

    @staticmethod
    def prepare(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if out.empty:
            return out
        out["date"] = out["datetime"].dt.date
        out["ohlc4"] = (out["open"] + out["high"] + out["low"] + out["close"]) / 4.0
        out["pv"] = out["ohlc4"] * out["volume"]
        out["cum_pv"] = out.groupby("date")["pv"].cumsum()
        out["cum_vol"] = out.groupby("date")["volume"].cumsum()
        out["vwap"] = out["cum_pv"] / out["cum_vol"].replace(0, pd.NA)
        out["prev_ohlc4"] = out["ohlc4"].shift(1)
        out["prev_close"] = out["close"].shift(1)
        out["prev_vwap"] = out["vwap"].shift(1)
        prev_close = out["close"].shift(1)
        tr = pd.concat(
            [(out["high"] - out["low"]),
             (out["high"] - prev_close).abs(),
             (out["low"] - prev_close).abs()], axis=1
        ).max(axis=1)
        out["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        return out

    @staticmethod
    def atr(df: pd.DataFrame, length=14) -> pd.Series:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1
        ).max(axis=1)
        return tr.ewm(alpha=1/length, adjust=False, min_periods=length).mean()
