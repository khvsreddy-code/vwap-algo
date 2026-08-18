from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class StrategyConfig:
    confirmation_points: float = 15.0
    confirmation_bars: int = 5


class VwapConfirmationEngine:
    """Python equivalent of the user's Pine VWAP-cross confirmation logic."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.cross_price: Optional[float] = None
        self.cross_bar: Optional[int] = None
        self.cross_direction: int = 0
        self.bar_index: int = -1
        self.trade_active: bool = False
        self.trade_direction: int = 0
        self.last_signal = None

    @staticmethod
    def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        if out.empty:
            return out
        out["date"] = out["datetime"].dt.date
        out["ohlc4"] = (out["open"] + out["high"] + out["low"] + out["close"]) / 4.0
        # Classic session VWAP. Requires meaningful volume.
        out["pv"] = out["ohlc4"] * out["volume"]
        out["cum_pv"] = out.groupby("date")["pv"].cumsum()
        out["cum_vol"] = out.groupby("date")["volume"].cumsum()
        out["vwap"] = out["cum_pv"] / out["cum_vol"].replace(0, pd.NA)
        return out

    def process_closed_candle(self, row: pd.Series):
        """Process one CLOSED candle. Returns BUY/SELL/None."""
        self.bar_index += 1
        close = float(row["close"])
        ohlc4 = float(row["ohlc4"])
        vwap = row["vwap"]
        if pd.isna(vwap):
            return None
        vwap = float(vwap)

        # Need the previous closed candle to detect a crossover exactly like Pine.
        prev_ohlc4 = row.get("prev_ohlc4")
        prev_vwap = row.get("prev_vwap")
        if prev_ohlc4 is None or prev_vwap is None or pd.isna(prev_ohlc4) or pd.isna(prev_vwap):
            return None

        new_long = float(prev_ohlc4) <= float(prev_vwap) and ohlc4 > vwap
        new_short = float(prev_ohlc4) >= float(prev_vwap) and ohlc4 < vwap

        if new_long:
            self.cross_price = vwap
            self.cross_bar = self.bar_index
            self.cross_direction = 1

        elif new_short:
            self.cross_price = vwap
            self.cross_bar = self.bar_index
            self.cross_direction = -1

        if self.cross_bar is None:
            return None

        bars_since = self.bar_index - self.cross_bar

        if 0 <= bars_since <= self.config.confirmation_bars:
            if self.cross_direction == 1 and close >= self.cross_price + self.config.confirmation_points:
                if not self.trade_active:
                    self.trade_active = True
                    self.trade_direction = 1
                    self.last_signal = {
                        "side": "BUY",
                        "entry": close,
                        "cross_price": self.cross_price,
                        "bars_since_cross": bars_since,
                    }
                    self._clear_setup()
                    return self.last_signal

            if self.cross_direction == -1 and close <= self.cross_price - self.config.confirmation_points:
                if not self.trade_active:
                    self.trade_active = True
                    self.trade_direction = -1
                    self.last_signal = {
                        "side": "SELL",
                        "entry": close,
                        "cross_price": self.cross_price,
                        "bars_since_cross": bars_since,
                    }
                    self._clear_setup()
                    return self.last_signal

        if bars_since > self.config.confirmation_bars:
            self._clear_setup()

        return None

    def _clear_setup(self):
        self.cross_price = None
        self.cross_bar = None
        self.cross_direction = 0
