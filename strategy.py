from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class StrategyConfig:
    confirmation_points: float = 15.0
    confirmation_bars: int = 5


class VwapConfirmationEngine:
    """Closed-candle implementation of the user's Pine VWAP confirmation rule."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.cross_price: Optional[float] = None
        self.cross_bar: Optional[int] = None
        self.cross_direction = 0
        self.bar_index = -1
        self.trade_active = False
        self.trade_direction = 0
        self.last_signal = None

    @property
    def bars_since_cross(self):
        if self.cross_bar is None:
            return None
        return self.bar_index - self.cross_bar

    def process_closed_candle(self, row: pd.Series):
        self.bar_index += 1
        close = float(row["close"])
        ohlc4 = float(row["ohlc4"])
        vwap = row.get("vwap")
        if pd.isna(vwap):
            return None
        vwap = float(vwap)
        prev_ohlc4 = row.get("prev_ohlc4")
        prev_vwap = row.get("prev_vwap")
        if prev_ohlc4 is None or prev_vwap is None or pd.isna(prev_ohlc4) or pd.isna(prev_vwap):
            return None

        long_cross = float(prev_ohlc4) <= float(prev_vwap) and ohlc4 > vwap
        short_cross = float(prev_ohlc4) >= float(prev_vwap) and ohlc4 < vwap
        if long_cross:
            self.cross_price = vwap
            self.cross_bar = self.bar_index
            self.cross_direction = 1
        elif short_cross:
            self.cross_price = vwap
            self.cross_bar = self.bar_index
            self.cross_direction = -1

        if self.cross_bar is None:
            return None
        bars = self.bar_index - self.cross_bar
        if 0 <= bars <= self.config.confirmation_bars:
            if self.cross_direction == 1 and close >= self.cross_price + self.config.confirmation_points:
                if not self.trade_active:
                    self.trade_active = True
                    self.trade_direction = 1
                    signal = {"side":"BUY", "entry":close, "cross_price":self.cross_price,
                              "bars_since_cross":bars, "time":row["datetime"]}
                    self.last_signal = signal
                    self._clear_setup()
                    return signal
            if self.cross_direction == -1 and close <= self.cross_price - self.config.confirmation_points:
                if not self.trade_active:
                    self.trade_active = True
                    self.trade_direction = -1
                    signal = {"side":"SELL", "entry":close, "cross_price":self.cross_price,
                              "bars_since_cross":bars, "time":row["datetime"]}
                    self.last_signal = signal
                    self._clear_setup()
                    return signal
        if bars > self.config.confirmation_bars:
            self._clear_setup()
        return None

    def reset_trade(self):
        self.trade_active = False
        self.trade_direction = 0

    def _clear_setup(self):
        self.cross_price = None
        self.cross_bar = None
        self.cross_direction = 0

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
        out["prev_vwap"] = out["vwap"].shift(1)
        prev_close = out["close"].shift(1)
        tr = pd.concat([(out["high"]-out["low"]), (out["high"]-prev_close).abs(), (out["low"]-prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        return out

    @staticmethod
    def atr(df: pd.DataFrame, length=14) -> pd.Series:
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/length, adjust=False, min_periods=length).mean()
