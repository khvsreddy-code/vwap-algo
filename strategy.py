from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class StrategyConfig:
    confirmation_points: float = 15.0
    confirmation_bars: int = 8

    # VWAP setup families.
    allow_vwap_interaction: bool = True
    vwap_interaction_tolerance: float = 0.5
    allow_vwap_bounce: bool = True
    allow_failed_cross: bool = True

    # Quality/chop protection. These do not change the 15-point confirmation
    # rule; they decide which VWAP relationships are worth arming.
    chop_lookback: int = 8
    max_chop_flips: int = 3
    strong_move_atr_fraction: float = 0.25

    # Market-regime protection. This is intentionally conservative: it blocks
    # only low-strength, flat-VWAP chop. It never imposes a daily trade quota,
    # so a normal day can still produce multiple independent VWAP cycles.
    use_regime_filter: bool = True
    adx_length: int = 14
    range_adx_threshold: float = 18.0
    range_vwap_slope_atr_fraction: float = 0.05
    range_min_bars: int = 14

    # v10 adaptive VWAP quality layer. These are deliberately soft and
    # setup-specific: they rank the setup rather than changing the 15-point /
    # 8-candle confirmation rule. Missing warm-up data never blocks a signal.
    use_quality_filter: bool = True
    min_quality_score: float = 4.0
    quality_lookback: int = 6
    min_trend_persistence: int = 3
    min_distance_atr: float = 0.15
    min_body_fraction: float = 0.20
    atr_expansion_ratio: float = 1.02
    continuation_quality_min: float = 4.0
    reclaim_quality_min: float = 3.5
    bounce_quality_min: float = 3.5
    failed_cross_quality_min: float = 3.0

    # Stale setup protection: once an armed cross has been invalidated by a
    # later decisive close through VWAP, it cannot fire several candles later.
    invalidate_on_opposite_close: bool = True
    opposite_close_tolerance: float = 0.25


class VwapConfirmationEngine:
    """
    VWAP confirmation state machine.

    Setup families:
      CLOSE_CROSS
        A completed candle closes across VWAP.

      VWAP_RECLAIM
        Price was on the opposite side of VWAP and the completed candle
        reclaims VWAP after trading through it.

      VWAP_BOUNCE
        Price is already on one side, pulls back to VWAP, then closes back
        on the same side. This catches rounded/doji/retest candles.

      FAILED_CROSS
        An armed cross fails on the next completed candle and closes back
        through VWAP. The failed side is discarded and the new side is armed.

    Confirmation remains unchanged:
      - The setup candle NEVER confirms itself.
      - The next N candles/ticks must travel the full configured distance
        from the setup candle's close.
      - Historical replay uses high/low so an intrabar move cannot be missed.
      - Live confirmation uses ticks so execution is not dependent on a
        Streamlit rerun.

    A chop filter suppresses weak setups when VWAP has been crossed repeatedly
    in a short range. Strong moves are allowed through the filter.
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.cross_price: Optional[float] = None
        self.cross_bar: Optional[int] = None
        self.cross_direction = 0
        self.confirmation_level: Optional[float] = None
        self.cross_type: Optional[str] = None
        self.setup_quality: Optional[str] = None
        self.bar_index = -1
        self.trade_active = False
        self.trade_direction = 0
        self.last_signal = None
        self.last_session_date = None
        self._recent_relationships = []

    @property
    def bars_since_cross(self):
        if self.cross_bar is None:
            return None
        return self.bar_index - self.cross_bar

    @property
    def armed(self):
        return self.cross_bar is not None and self.cross_direction != 0 and not self.trade_active

    def _arm(self, direction, close, cross_type="CLOSE_CROSS", quality=None):
        self.cross_price = float(close)
        self.cross_bar = self.bar_index
        self.cross_direction = int(direction)
        self.cross_type = str(cross_type)
        self.setup_quality = quality or cross_type
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
        self.cross_type = None
        self.setup_quality = None

    def _make_signal(self, side, entry, row=None, bars=None):
        entry = float(entry)
        level = float(self.confirmation_level)
        if side == "BUY" and entry < level:
            return None
        if side == "SELL" and entry > level:
            return None

        self.trade_active = True
        self.trade_direction = 1 if side == "BUY" else -1
        signal_score = None
        signal_quality = self.setup_quality or self.cross_type or "CLOSE_CROSS"
        if row is not None:
            try:
                signal_score, _ = self._quality_score(
                    row, self.trade_direction, self.cross_type or "CLOSE_CROSS"
                )
            except Exception:
                signal_score = None
        signal = {
            "side": side,
            "entry": float(entry),
            "cross_price": float(self.cross_price),
            "confirmation_level": float(self.confirmation_level),
            "cross_type": self.cross_type or "CLOSE_CROSS",
            "setup_quality": signal_quality,
            "quality_score": float(signal_score) if signal_score is not None and signal_score < 900 else None,
            "bars_since_cross": int(
                bars if bars is not None else max(0, self.bar_index - self.cross_bar)
            ),
            "time": row["datetime"] if row is not None else pd.Timestamp.now(tz="Asia/Kolkata"),
        }
        self.last_signal = signal
        self._clear_setup()
        return signal

    def _maybe_expire(self, current_bar):
        if self.cross_bar is not None and current_bar - self.cross_bar > self.config.confirmation_bars:
            self._clear_setup()

    @staticmethod
    def _safe_float(value):
        try:
            x = float(value)
            return x if pd.notna(x) else None
        except (TypeError, ValueError):
            return None

    def _chop_blocks(self, row, direction):
        """Reject weak signals when VWAP is being crossed repeatedly in a range."""
        lookback = max(2, int(self.config.chop_lookback))
        if len(self._recent_relationships) < lookback:
            return False

        flips = sum(
            1 for a, b in zip(self._recent_relationships[-lookback:-1],
                              self._recent_relationships[-lookback + 1:])
            if a and b and a != b
        )
        if flips < int(self.config.max_chop_flips):
            return False

        close = self._safe_float(row.get("close"))
        vwap = self._safe_float(row.get("vwap"))
        atr = self._safe_float(row.get("atr"))
        if close is None or vwap is None:
            return True

        # A decisive displacement away from VWAP overrides chop protection.
        if atr and atr > 0 and abs(close - vwap) >= atr * float(self.config.strong_move_atr_fraction):
            return False

        # Otherwise, the market is oscillating around VWAP: do not add another
        # low-quality setup. Existing armed confirmations are never blocked.
        return True

    def _record_relationship(self, close, vwap, tol):
        if close is None or vwap is None:
            self._recent_relationships.append(0)
        elif close > vwap + tol:
            self._recent_relationships.append(1)
        elif close < vwap - tol:
            self._recent_relationships.append(-1)
        else:
            self._recent_relationships.append(0)

        keep = max(12, int(self.config.chop_lookback) + 2)
        self._recent_relationships = self._recent_relationships[-keep:]

    def _market_regime(self, row):
        """Conservative regime classifier; never imposes a daily trade quota."""
        if not self.config.use_regime_filter:
            return "UNKNOWN"
        adx = self._safe_float(row.get("adx"))
        atr = self._safe_float(row.get("atr"))
        vwap = self._safe_float(row.get("vwap"))
        slope = self._safe_float(row.get("vwap_slope"))
        close = self._safe_float(row.get("close"))
        if any(x is None for x in (adx, vwap, close)):
            return "UNKNOWN"
        if adx >= self.config.range_adx_threshold:
            if close > vwap and (slope is None or slope >= 0):
                return "TREND_BULL"
            if close < vwap and (slope is None or slope <= 0):
                return "TREND_BEAR"
        if adx < self.config.range_adx_threshold:
            flat = True if slope is None else (atr is None or atr <= 0 or abs(slope) <= atr * self.config.range_vwap_slope_atr_fraction)
            lb = max(2, int(self.config.chop_lookback))
            flips = sum(1 for a,b in zip(self._recent_relationships[-lb:-1], self._recent_relationships[-lb+1:]) if a and b and a != b)
            if flat and flips >= self.config.max_chop_flips:
                return "RANGE"
        return "UNKNOWN"

    def regime(self, row):
        return self._market_regime(row)

    def _relationship_stats(self, direction):
        """Return persistence and recent flip count from already-seen candles."""
        lookback = max(3, int(self.config.quality_lookback))
        rel = self._recent_relationships[-lookback:]
        persistence = 0
        for value in reversed(rel):
            if value == direction:
                persistence += 1
            else:
                break
        flips = sum(
            1 for a, b in zip(rel[:-1], rel[1:])
            if a and b and a != b
        )
        return persistence, flips

    def _quality_score(self, row, direction, setup_type):
        """
        Score a VWAP setup using only information available on that candle.

        Score components:
          - VWAP slope alignment
          - ADX / directional-strength agreement
          - normalized distance from VWAP
          - candle body/close quality
          - same-side persistence
          - recent VWAP cleanliness
          - volatility expansion

        The score is intentionally conservative: if a component is unavailable
        during indicator warm-up it contributes nothing instead of rejecting
        the setup. This prevents replay/live differences.
        """
        if not self.config.use_quality_filter:
            return 999.0, "UNFILTERED"

        close = self._safe_float(row.get("close"))
        open_price = self._safe_float(row.get("open", close))
        high = self._safe_float(row.get("high", close))
        low = self._safe_float(row.get("low", close))
        vwap = self._safe_float(row.get("vwap"))
        atr = self._safe_float(row.get("atr"))
        slope = self._safe_float(row.get("vwap_slope"))
        adx = self._safe_float(row.get("adx"))

        score = 0.0
        available = 0

        # 1) VWAP slope alignment: strongest when VWAP itself agrees.
        if slope is not None and atr is not None and atr > 0:
            available += 1
            slope_norm = slope / atr
            if direction == 1 and slope_norm > 0.02:
                score += 2.0
            elif direction == -1 and slope_norm < -0.02:
                score += 2.0
            elif abs(slope_norm) <= 0.02:
                score += 0.25

        # 2) Directional strength. ADX is strength, not direction; direction is
        # checked from price relative to VWAP.
        if adx is not None:
            available += 1
            if adx >= 25:
                score += 1.5
            elif adx >= 20:
                score += 1.0
            elif adx >= 15:
                score += 0.5

        # 3) Normalized displacement from VWAP. A meaningful move is more
        # trustworthy than a 1-2 point touch.
        if close is not None and vwap is not None and atr is not None and atr > 0:
            available += 1
            distance = abs(close - vwap) / atr
            if distance >= max(0.25, float(self.config.min_distance_atr) * 2):
                score += 2.0
            elif distance >= float(self.config.min_distance_atr):
                score += 1.0

        # 4) Candle quality: body and close location. Dojis are not rejected;
        # they can still score from the other components and remain valid for
        # the rounded/reclaim path.
        if open_price is not None and high is not None and low is not None and close is not None:
            available += 1
            rng = max(high - low, 1e-9)
            body_fraction = abs(close - open_price) / rng
            close_location = ((close - low) / rng) if direction == 1 else ((high - close) / rng)
            if body_fraction >= float(self.config.min_body_fraction):
                score += 1.0
            if close_location >= 0.70:
                score += 1.0
            elif close_location >= 0.55:
                score += 0.5

        # 5) Persistence: repeated same-side closes are useful for continuation,
        # but not required for a fresh reclaim/failed-cross.
        persistence, flips = self._relationship_stats(direction)
        if persistence >= int(self.config.min_trend_persistence):
            score += 1.0
        elif persistence >= 2:
            score += 0.5

        # 6) Penalize recent alternating VWAP behavior, but allow strong
        # displacement and reversal-specific setups to survive.
        if flips >= 3:
            score -= 1.0
        elif flips == 2:
            score -= 0.25

        # 7) Volatility expansion: compare current ATR with recent ATR values if
        # the caller supplied a history column.
        atr_recent = row.get("atr_mean")
        atr_recent_f = self._safe_float(atr_recent)
        if atr is not None and atr_recent_f is not None and atr_recent_f > 0:
            available += 1
            if atr / atr_recent_f >= float(self.config.atr_expansion_ratio):
                score += 1.0

        # Failed/reversal setups get a small structural bonus because they are
        # intentionally different from ordinary trend continuation.
        if setup_type in {"FAILED_CROSS", "VWAP_RECLAIM", "VWAP_INTERACTION"}:
            score += 0.5

        # If there is insufficient indicator data, preserve the old behavior.
        if available < 3:
            return 999.0, "WARMUP"

        threshold = {
            "CLOSE_CROSS": float(self.config.continuation_quality_min),
            "VWAP_RECLAIM": float(self.config.reclaim_quality_min),
            "VWAP_INTERACTION": float(self.config.reclaim_quality_min),
            "VWAP_BOUNCE": float(self.config.bounce_quality_min),
            "FAILED_CROSS": float(self.config.failed_cross_quality_min),
        }.get(setup_type, float(self.config.min_quality_score))

        if score >= threshold + 2:
            label = "HIGH"
        elif score >= threshold:
            label = "GOOD"
        else:
            label = "WEAK"
        return score, label

    def _quality_allows(self, row, direction, setup_type):
        score, label = self._quality_score(row, direction, setup_type)
        if label in {"WARMUP", "HIGH", "GOOD", "UNFILTERED"}:
            return True, score, label
        return False, score, label

    def _detect_vwap_cross(self, row, first_session_candle=False):
        """Return (direction, setup_type, quality) for a completed candle."""
        try:
            close = float(row["close"])
            open_price = float(row.get("open", close))
            high = float(row.get("high", close))
            low = float(row.get("low", close))
            vwap = row.get("vwap")
            if pd.isna(vwap):
                return None
            vwap = float(vwap)

            prev_close = row.get("prev_close")
            prev_vwap = row.get("prev_vwap")
            prev_close_f = self._safe_float(prev_close)
            prev_vwap_f = self._safe_float(prev_vwap)
            tol = max(0.0, float(self.config.vwap_interaction_tolerance))

            # Strict close-to-close cross.
            if first_session_candle or prev_close_f is None or prev_vwap_f is None:
                long_cross = open_price <= vwap and close > vwap
                short_cross = open_price >= vwap and close < vwap
            else:
                # A candle that opens through VWAP after price was already on
                # the same side is a retest/bounce, not a fresh cross. Reserve
                # CLOSE_CROSS for a genuine side change.
                long_cross = prev_close_f <= prev_vwap_f and close > vwap
                short_cross = prev_close_f >= prev_vwap_f and close < vwap

            if long_cross:
                if not self._chop_blocks(row, 1) and self._market_regime(row) != "RANGE":
                    allowed, score, q = self._quality_allows(row, 1, "CLOSE_CROSS")
                    if allowed:
                        return (1, "CLOSE_CROSS", q)
                return None
            if short_cross:
                if not self._chop_blocks(row, -1) and self._market_regime(row) != "RANGE":
                    allowed, score, q = self._quality_allows(row, -1, "CLOSE_CROSS")
                    if allowed:
                        return (-1, "CLOSE_CROSS", q)
                return None

            if not self.config.allow_vwap_interaction:
                return None

            straddles = low <= vwap + tol and high >= vwap - tol
            if not straddles:
                return None

            prev_below = (
                prev_close_f is not None and prev_vwap_f is not None
                and prev_close_f < prev_vwap_f - tol
            )
            prev_above = (
                prev_close_f is not None and prev_vwap_f is not None
                and prev_close_f > prev_vwap_f + tol
            )

            # Reclaim: previously below/above, now closes back through VWAP.
            bullish_reclaim = (
                close >= vwap - tol
                and (prev_below or (open_price < vwap - tol and close >= vwap - tol))
            )
            bearish_reclaim = (
                close <= vwap + tol
                and (prev_above or (open_price > vwap + tol and close <= vwap + tol))
            )

            # Bounce/retest: already on one side, VWAP is tested, and the close
            # holds the same side. Small-body candles are valid if the test is
            # genuine; this is the rounded-candle case from the user's chart.
            bullish_bounce = (
                self.config.allow_vwap_bounce
                and prev_close_f is not None and prev_vwap_f is not None
                and prev_close_f > prev_vwap_f + tol
                and low <= vwap - tol
                and high >= vwap + tol
                and close >= vwap + tol
                and close >= open_price
            )
            bearish_bounce = (
                self.config.allow_vwap_bounce
                and prev_close_f is not None and prev_vwap_f is not None
                and prev_close_f < prev_vwap_f - tol
                and high >= vwap + tol
                and low <= vwap - tol
                and close <= vwap - tol
                and close <= open_price
            )

            # Preserve the earlier VWAP_INTERACTION label for small-body
            # rounded/doji candles that reclaim/reject very close to VWAP.
            body = abs(close - open_price)
            range_size = max(high - low, 1e-9)
            rounded = body <= range_size * 0.25

            if bullish_reclaim and not self._chop_blocks(row, 1) and self._market_regime(row) != "RANGE":
                stype = "VWAP_INTERACTION" if rounded else "VWAP_RECLAIM"
                allowed, score, q = self._quality_allows(row, 1, stype)
                if allowed:
                    return (1, stype, q)
            if bearish_reclaim and not self._chop_blocks(row, -1) and self._market_regime(row) != "RANGE":
                stype = "VWAP_INTERACTION" if rounded else "VWAP_RECLAIM"
                allowed, score, q = self._quality_allows(row, -1, stype)
                if allowed:
                    return (-1, stype, q)
            if bullish_bounce and not self._chop_blocks(row, 1) and self._market_regime(row) != "RANGE":
                allowed, score, q = self._quality_allows(row, 1, "VWAP_BOUNCE")
                if allowed:
                    return (1, "VWAP_BOUNCE", q)
            if bearish_bounce and not self._chop_blocks(row, -1) and self._market_regime(row) != "RANGE":
                allowed, score, q = self._quality_allows(row, -1, "VWAP_BOUNCE")
                if allowed:
                    return (-1, "VWAP_BOUNCE", q)

            return None
        except (TypeError, ValueError):
            return None

    def _update_relationship_state(self, row):
        close = self._safe_float(row.get("close"))
        vwap = self._safe_float(row.get("vwap"))
        if close is not None and vwap is not None:
            self._record_relationship(close, vwap, max(0.0, float(self.config.vwap_interaction_tolerance)))

    def seed_from_history(self, df: pd.DataFrame):
        """Rebuild pending state from already-closed history without emitting orders."""
        self.cross_price = None
        self.cross_bar = None
        self.cross_direction = 0
        self.confirmation_level = None
        self.cross_type = None
        self.setup_quality = None
        self.trade_active = False
        self.trade_direction = 0
        self.last_signal = None
        self.bar_index = -1
        self.last_session_date = None
        self._recent_relationships = []

        if df is None or df.empty:
            return

        previous_date = None
        for _, row in df.reset_index(drop=True).iterrows():
            self.bar_index += 1

            row_date = row.get("date")
            if pd.isna(row_date):
                try:
                    row_date = pd.Timestamp(row["datetime"]).date()
                except Exception:
                    row_date = None

            first_session_candle = previous_date is None or row_date != previous_date
            if previous_date is not None and row_date != previous_date:
                self._clear_setup()
                self._recent_relationships = []

            previous_date = row_date
            self.last_session_date = row_date

            vwap = row.get("vwap")
            if pd.isna(vwap):
                self._update_relationship_state(row)
                continue

            # Existing setup gets first right to confirmation.
            if self.cross_bar is not None:
                bars = self.bar_index - self.cross_bar
                if 1 <= bars <= self.config.confirmation_bars:
                    if self.config.invalidate_on_opposite_close and bars > 1:
                        v = self._safe_float(row.get("vwap"))
                        close_value = self._safe_float(row.get("close"))
                        if v is not None and close_value is not None:
                            tol = float(self.config.opposite_close_tolerance)
                            if ((self.cross_direction == 1 and close_value < v - tol) or
                                (self.cross_direction == -1 and close_value > v + tol)):
                                self._clear_setup()
                                self._update_relationship_state(row)
                                continue
                    confirmed = (
                        self.cross_direction == 1 and float(row["high"]) >= float(self.confirmation_level)
                    ) or (
                        self.cross_direction == -1 and float(row["low"]) <= float(self.confirmation_level)
                    )
                    if confirmed:
                        self._clear_setup()
                        self._update_relationship_state(row)
                        continue

                # Optional failed-cross reversal. If a setup immediately fails
                # through VWAP, replace it with the opposite setup rather than
                # carrying the stale direction for the rest of the window.
                if self.config.allow_failed_cross and bars == 1:
                    close = float(row["close"])
                    v = float(row["vwap"])
                    if self.cross_direction == 1 and close < v:
                        self._clear_setup()
                        self._arm(-1, close, "FAILED_CROSS", "MEDIUM")
                    elif self.cross_direction == -1 and close > v:
                        self._clear_setup()
                        self._arm(1, close, "FAILED_CROSS", "MEDIUM")
                    self._update_relationship_state(row)
                    continue

                if bars > self.config.confirmation_bars:
                    self._clear_setup()

            if self.cross_bar is None:
                detected = self._detect_vwap_cross(row, first_session_candle)
                if detected:
                    direction, setup_type, quality = detected
                    self._arm(direction, float(row["close"]), setup_type, quality)

            self._update_relationship_state(row)

        self.last_session_date = previous_date

    def process_closed_candle(self, row: pd.Series):
        self.bar_index += 1
        close = float(row["close"])
        high = float(row.get("high", close))
        low = float(row.get("low", close))
        vwap = row.get("vwap")
        row_date = row.get("date")
        if pd.isna(row_date):
            try:
                row_date = pd.Timestamp(row["datetime"]).date()
            except Exception:
                row_date = None

        first_session_candle = (
            bool(row.get("_algo_session_first", False))
            or self.last_session_date is None
            or row_date != self.last_session_date
        )
        if first_session_candle:
            self._clear_setup()
            self._recent_relationships = []
        self.last_session_date = row_date

        if pd.isna(vwap):
            self._maybe_expire(self.bar_index)
            return None

        # Pending setup is checked before any new setup.
        if self.cross_bar is not None and not self.trade_active:
            bars = self.bar_index - self.cross_bar

            if 1 <= bars <= self.config.confirmation_bars:
                if self.config.invalidate_on_opposite_close and bars > 1:
                    v = self._safe_float(vwap)
                    if v is not None:
                        tol = float(self.config.opposite_close_tolerance)
                        if ((self.cross_direction == 1 and close < v - tol) or
                            (self.cross_direction == -1 and close > v + tol)):
                            self._clear_setup()
                            self._update_relationship_state(row)
                            return None

                if self.cross_direction == 1 and high >= float(self.confirmation_level):
                    signal = self._make_signal("BUY", self.confirmation_level, row, bars)
                    self._update_relationship_state(row)
                    return signal
                if self.cross_direction == -1 and low <= float(self.confirmation_level):
                    signal = self._make_signal("SELL", self.confirmation_level, row, bars)
                    self._update_relationship_state(row)
                    return signal

            # Failed cross is deliberately only recognized on the first candle
            # after the original setup. This avoids flipping direction repeatedly
            # in ordinary VWAP chop.
            if self.config.allow_failed_cross and bars == 1:
                if self.cross_direction == 1 and close < float(vwap):
                    self._clear_setup()
                    self._arm(-1, close, "FAILED_CROSS", "MEDIUM")
                    self._update_relationship_state(row)
                    return None
                if self.cross_direction == -1 and close > float(vwap):
                    self._clear_setup()
                    self._arm(1, close, "FAILED_CROSS", "MEDIUM")
                    self._update_relationship_state(row)
                    return None

            self._maybe_expire(self.bar_index)
            if self.cross_bar is not None:
                self._update_relationship_state(row)
                return None

        detected = self._detect_vwap_cross(row, first_session_candle)
        if detected:
            direction, setup_type, quality = detected
            self._arm(direction, close, setup_type, quality)

        self._update_relationship_state(row)
        return None

    def process_live_candle(self, row: pd.Series):
        """Confirm an already-armed setup from the currently forming candle."""
        if row is None or self.trade_active:
            return None

        high = float(row.get("high"))
        low = float(row.get("low"))
        if pd.isna(row.get("vwap")):
            return None

        row_date = row.get("date")
        if pd.isna(row_date):
            try:
                row_date = pd.Timestamp(row.get("datetime")).date()
            except Exception:
                row_date = None
        first_session_candle = (
            bool(row.get("_algo_session_first", False))
            or self.last_session_date is None
            or row_date != self.last_session_date
        )
        if first_session_candle:
            self._clear_setup()
            self._recent_relationships = []
            self.last_session_date = row_date

        current_bar = self.bar_index + 1

        if self.cross_bar is not None:
            bars = current_bar - self.cross_bar
            if 1 <= bars <= self.config.confirmation_bars:
                if self.config.invalidate_on_opposite_close and bars > 1:
                    v = self._safe_float(vwap)
                    if v is not None:
                        tol = float(self.config.opposite_close_tolerance)
                        if ((self.cross_direction == 1 and close < v - tol) or
                            (self.cross_direction == -1 and close > v + tol)):
                            self._clear_setup()
                            self._update_relationship_state(row)
                            return None

                if self.cross_direction == 1 and high >= float(self.confirmation_level):
                    return self._make_signal(
                        "BUY", float(self.confirmation_level),
                        {"datetime": row.get("datetime")}, bars
                    )
                if self.cross_direction == -1 and low <= float(self.confirmation_level):
                    return self._make_signal(
                        "SELL", float(self.confirmation_level),
                        {"datetime": row.get("datetime")}, bars
                    )
            elif bars > self.config.confirmation_bars:
                self._clear_setup()

        return None

    def process_live_tick(self, ltp, timestamp=None):
        """Confirm the pending setup immediately when a live tick reaches its level."""
        if self.trade_active or self.cross_bar is None:
            return None

        if timestamp is not None:
            try:
                ts = pd.Timestamp(timestamp)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("Asia/Kolkata")
                else:
                    ts = ts.tz_convert("Asia/Kolkata")
                tick_date = ts.date()
                if self.last_session_date is not None and tick_date != self.last_session_date:
                    self._clear_setup()
                    self.last_session_date = tick_date
                    self.bar_index += 1
                    return None
            except Exception:
                pass

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
                "BUY", ltp,
                {"datetime": timestamp or pd.Timestamp.now(tz="Asia/Kolkata")}, bars
            )
        if self.cross_direction == -1 and ltp <= float(self.confirmation_level):
            return self._make_signal(
                "SELL", ltp,
                {"datetime": timestamp or pd.Timestamp.now(tz="Asia/Kolkata")}, bars
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
        # Directional movement / ADX.  This is used only as a conservative
        # range filter; NaN during warm-up means UNKNOWN, never forced NO-TRADE.
        up_move = out["high"].diff()
        down_move = -out["low"].diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        atr_safe = out["atr"].replace(0, pd.NA)
        plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_safe
        minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False, min_periods=14).mean() / atr_safe
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, pd.NA)
        out["adx"] = dx.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
        out["vwap_slope"] = out["vwap"].diff()
        out["atr_mean"] = out["atr"].rolling(window=14, min_periods=8).mean()
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
