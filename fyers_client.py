from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd
import threading
import time

IST = ZoneInfo("Asia/Kolkata")


class _FyersHistoryRateLimiter:
    """Process-wide limiter/cooldown for FYERS History REST calls."""
    _lock = threading.RLock()
    _next_allowed = 0.0
    _cooldown_until = 0.0

    @classmethod
    def wait(cls, minimum_interval=0.35):
        while True:
            with cls._lock:
                now = time.monotonic()
                delay = max(cls._next_allowed, cls._cooldown_until) - now
                if delay <= 0:
                    cls._next_allowed = now + float(minimum_interval)
                    return
            time.sleep(min(delay, 1.0))

    @classmethod
    def rate_limited(cls, cooldown):
        with cls._lock:
            cls._cooldown_until = max(
                cls._cooldown_until, time.monotonic() + float(cooldown)
            )



# Keep the market-data and order websocket imports independent.
# Some fyers-apiv3 builds expose data_ws but do not expose order_ws.  Importing
# both in one statement makes an optional order-websocket import failure set
# data_ws=None too, which silently kills the live candle feed.
try:
    from fyers_apiv3 import fyersModel
except ImportError:
    fyersModel = None

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None

try:
    from fyers_apiv3.FyersWebsocket import order_ws
except ImportError:
    order_ws = None


class FyersClient:
    """FYERS API v3 wrapper used by the Streamlit terminal."""

    def __init__(self, app_id: str, access_token: str):
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3: pip install fyers-apiv3")
        self.app_id = (app_id or "").strip()
        self.access_token = (access_token or "").strip()
        if not self.app_id or not self.access_token:
            raise ValueError("App ID and access token are required")
        self.rest = fyersModel.FyersModel(
            client_id=self.app_id,
            token=self.access_token,
            is_async=False,
            log_path="",
        )

    @staticmethod
    def auth_url(app_id, secret_id, redirect_uri, state="fyers_vwap"):
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3")
        session = fyersModel.SessionModel(
            client_id=app_id.strip(), secret_key=secret_id.strip(),
            redirect_uri=redirect_uri.strip(), response_type="code",
            state=state, grant_type="authorization_code",
        )
        return session.generate_authcode()

    @staticmethod
    def exchange_auth_code(app_id, secret_id, redirect_uri, auth_code):
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3")
        session = fyersModel.SessionModel(
            client_id=app_id.strip(), secret_key=secret_id.strip(),
            redirect_uri=redirect_uri.strip(), response_type="code",
            grant_type="authorization_code",
        )
        session.set_token(auth_code.strip())
        response = session.generate_token()
        if response.get("s") == "error":
            raise RuntimeError(str(response))
        token = response.get("access_token")
        if not token:
            raise RuntimeError(f"No access_token returned: {response}")
        return token

    def _check(self, response, name):
        if not isinstance(response, dict):
            raise RuntimeError(f"{name}: unexpected response {response!r}")
        if response.get("s") != "ok":
            code = response.get("code")
            msg = response.get("message", "Unknown FYERS error")
            if code in (-8, -15, -16, -17) or code == 401:
                raise RuntimeError(
                    f"{name}: FYERS authentication failed ({code}): {msg}. "
                    "Generate a fresh v3 access token and make sure the App ID matches it."
                )
            raise RuntimeError(f"{name}: {response}")
        return response

    def profile(self): return self._check(self.rest.get_profile(), "Profile")
    def funds(self): return self._check(self.rest.funds(), "Funds")
    def positions(self): return self._check(self.rest.positions(), "Positions")
    def holdings(self): return self._check(self.rest.holdings(), "Holdings")
    def orders(self): return self._check(self.rest.orderbook(), "Orderbook")
    def trades(self): return self._check(self.rest.tradebook(), "Tradebook")

    def history(self, symbol, resolution, days=31, oi_flag=False):
        """Fetch intraday history with serialized, 429-aware REST access."""
        end_dt = datetime.now(IST)
        start_dt = end_dt - timedelta(days=max(1, int(days)))
        is_continuous_future = "FUT" in str(symbol).upper()

        chunk_days = 7
        windows = []
        cursor = start_dt
        while cursor < end_dt:
            nxt = min(cursor + timedelta(days=chunk_days), end_dt)
            windows.append((cursor, nxt))
            cursor = nxt

        all_candles = []
        last_error = None

        for win_start, win_end in windows:
            data = {
                "symbol": symbol,
                "resolution": str(resolution),
                "date_format": "1",
                "range_from": win_start.date().isoformat(),
                "range_to": win_end.date().isoformat(),
            }
            if is_continuous_future:
                data["cont_flag"] = "1"
            if oi_flag:
                data["oi_flag"] = "1"

            for attempt in range(4):
                try:
                    _FyersHistoryRateLimiter.wait(0.35)
                    resp = self._check(self.rest.history(data=data), "History")
                    payload = resp.get("data", resp)
                    chunk = payload.get("candles", []) if isinstance(payload, dict) else resp.get("candles", [])
                    last_error = None
                    if chunk:
                        all_candles.extend(chunk)
                    break
                except Exception as exc:
                    last_error = exc
                    if not self._is_rate_limit_error(exc):
                        break
                    cooldown = min(60.0, 8.0 * (2 ** attempt))
                    _FyersHistoryRateLimiter.rate_limited(cooldown)
                    if attempt < 3:
                        time.sleep(min(cooldown, 2.0))
            # Even successful requests are paced so a 31-day bootstrap does not
            # become a burst when several Streamlit sessions connect together.

        if not all_candles:
            if last_error is not None:
                raise last_error
            raise RuntimeError(
                f"FYERS History returned no candles for {symbol} "
                f"(resolution={resolution}, range={start_dt.date()}..{end_dt.date()})."
            )

        max_cols = max((len(row) for row in all_candles if isinstance(row, (list, tuple))), default=6)
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        if oi_flag and max_cols >= 7:
            columns.append("oi")
        normalized = []
        for row in all_candles:
            vals = list(row) if isinstance(row, (list, tuple)) else []
            vals += [None] * max(0, len(columns) - len(vals))
            normalized.append(vals[:len(columns)])
        df = pd.DataFrame(normalized, columns=columns)
        df["datetime"] = pd.to_datetime(
            pd.to_numeric(df["timestamp"], errors="coerce"),
            unit="s", utc=True, errors="coerce"
        ).dt.tz_convert(IST)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if "oi" in df.columns:
            df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
        df = (
            df.dropna(subset=["datetime", "open", "high", "low", "close"])
              .sort_values("datetime")
              .drop_duplicates(subset=["datetime"], keep="last")
              .reset_index(drop=True)
        )
        if df.empty:
            raise RuntimeError(f"FYERS History returned no valid candles for {symbol}.")
        return df.drop(columns="timestamp", errors="ignore")

    @staticmethod
    def _is_rate_limit_error(exc):
        text = str(exc).lower()
        return any(token in text for token in (
            "429", "request limit", "rate limit", "too many requests",
            "rate_limited", "rate-limit",
        ))

    def history_for_date(self, symbol, resolution, trading_date, oi_flag=False, signal_symbol=None):
        """Fetch a complete IST trading session, with targeted gap repair.

        FYERS may occasionally return a successful-but-partial chunk when the
        History endpoint is slow or a request crosses a transient backend
        issue.  A successful HTTP response is therefore NOT treated as proof
        that the chunk is complete.

        The normal path uses four bounded session chunks.  For the NIFTY index
        (the chart/strategy symbol), the result is then checked against the
        expected one-minute timestamps and any missing runs are fetched again
        with narrow requests.  This repairs candle gaps without multiplying
        every option contract into hundreds of extra REST calls.

        Persisted recovery data is hard-bounded to:
            09:15 <= candle_start < 15:30 IST
        """
        day = pd.Timestamp(trading_date)
        if day.tzinfo is not None:
            day = day.tz_convert(IST).normalize().tz_localize(None)
        day = day.normalize()

        session_start = day.tz_localize(IST) + pd.Timedelta(hours=9, minutes=15)
        session_end = day.tz_localize(IST) + pd.Timedelta(hours=15, minutes=30)

        def _request(req, attempts=5):
            """Return candles, retrying transient REST failures safely."""
            last_error = None
            for attempt in range(attempts):
                try:
                    _FyersHistoryRateLimiter.wait(0.35)
                    resp = self._check(self.rest.history(data=req), "History")
                    payload = resp.get("data", resp)
                    candidate = (
                        payload.get("candles", [])
                        if isinstance(payload, dict)
                        else resp.get("candles", [])
                    )
                    return candidate or []
                except Exception as exc:
                    last_error = exc
                    lower = str(exc).lower()
                    transient = self._is_rate_limit_error(exc) or any(token in lower for token in (
                        "timeout", "timed out", "temporarily unavailable",
                        "connection reset", "connection aborted",
                        "remote host was lost", "502", "503", "504",
                        "server error", "gateway",
                    ))
                    if not transient or attempt >= attempts - 1:
                        raise
                    if self._is_rate_limit_error(exc):
                        cooldown = min(30.0, 4.0 * (2 ** attempt))
                        _FyersHistoryRateLimiter.rate_limited(cooldown)
                        time.sleep(min(cooldown, 2.0))
                    else:
                        time.sleep(min(6.0, 0.75 * (2 ** attempt)))
            if last_error is not None:
                raise last_error
            return []

        # Two-hour-ish primary chunks keep the provider response bounded while
        # avoiding the 30-minute request explosion that made Complete Day slow.
        chunk_starts = [
            session_start,
            session_start + pd.Timedelta(hours=2),
            session_start + pd.Timedelta(hours=4),
            session_start + pd.Timedelta(hours=5, minutes=30),
        ]
        chunk_ends = [
            min(session_start + pd.Timedelta(hours=2), session_end),
            min(session_start + pd.Timedelta(hours=4), session_end),
            min(session_start + pd.Timedelta(hours=5, minutes=30), session_end),
            session_end,
        ]

        requests = []
        for chunk_start, chunk_end in zip(chunk_starts, chunk_ends):
            if chunk_start >= chunk_end:
                continue
            req = {
                "symbol": symbol,
                "resolution": str(resolution),
                "date_format": "0",
                "range_from": str(int(chunk_start.timestamp())),
                "range_to": str(int(chunk_end.timestamp())),
            }
            if "FUT" in str(symbol).upper():
                req["cont_flag"] = "1"
            if oi_flag:
                req["oi_flag"] = "1"
            requests.append(req)

        all_candles = []
        errors = []

        for req in requests:
            try:
                candles = _request(req)
            except Exception as exc:
                text = str(exc).lower()
                if "no_data" in text or "no data" in text:
                    candles = []
                else:
                    errors.append(exc)
                    candles = []
            if candles:
                all_candles.extend(candles)
            # Keep the process-wide FYERS limiter authoritative; this small
            # pause is only an additional cushion between sequential chunks.
            time.sleep(0.12)

        if not all_candles:
            if errors:
                raise errors[-1]
            return pd.DataFrame()

        max_cols = max(
            (len(row) for row in all_candles if isinstance(row, (list, tuple))),
            default=6,
        )
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        if oi_flag and max_cols >= 7:
            columns.append("oi")

        def _normalize(candles):
            normalized = []
            for row in candles:
                vals = list(row) if isinstance(row, (list, tuple)) else []
                if len(vals) < len(columns):
                    vals += [None] * (len(columns) - len(vals))
                normalized.append(vals[:len(columns)])
            if not normalized:
                return pd.DataFrame(columns=columns + ["datetime"])
            frame = pd.DataFrame(normalized, columns=columns)
            frame["datetime"] = pd.to_datetime(
                pd.to_numeric(frame["timestamp"], errors="coerce"),
                unit="s",
                utc=True,
                errors="coerce",
            ).dt.tz_convert(IST)
            for col in ["open", "high", "low", "close", "volume"]:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            if "oi" in frame.columns:
                frame["oi"] = pd.to_numeric(frame["oi"], errors="coerce")
            return frame

        df = _normalize(all_candles)
        df = df[
            (df["datetime"] >= session_start) &
            (df["datetime"] < session_end)
        ]
        df = (
            df.dropna(subset=["datetime", "open", "high", "low", "close"])
              .sort_values("datetime")
              .drop_duplicates(subset=["datetime"], keep="last")
              .reset_index(drop=True)
        )

        # IMPORTANT: only repair the NIFTY/index series. Options can legitimately
        # have sparse candles because there may be no trade in a minute. For NIFTY
        # we expect every one-minute slot during the elapsed regular session.
        if (
            not df.empty
            and signal_symbol is not None
            and str(symbol).strip() == str(signal_symbol).strip()
            and str(resolution) == "1"
        ):
            now_ist = pd.Timestamp.now(tz=IST)
            effective_end = session_end
            if day.date() == now_ist.date():
                # Never request future candles when Complete Day is pressed during
                # market hours.  15:30 itself is outside the canonical session.
                effective_end = min(session_end, now_ist.floor("min"))
            if effective_end > session_start:
                expected = pd.date_range(
                    start=session_start,
                    end=effective_end - pd.Timedelta(minutes=1),
                    freq="1min",
                    tz=IST,
                )
                present = set(pd.DatetimeIndex(df["datetime"]).tolist())
                missing = [ts for ts in expected if ts not in present]

                if missing:
                    # Collapse missing minutes into contiguous runs, then split
                    # very long runs into <=30-minute repair requests. This is
                    # deliberately targeted: a normal complete day costs the
                    # original four calls; extra calls occur only for real gaps.
                    runs = []
                    run_start = run_prev = missing[0]
                    for stamp in missing[1:]:
                        if stamp - run_prev > pd.Timedelta(minutes=1):
                            runs.append((run_start, run_prev + pd.Timedelta(minutes=1)))
                            run_start = stamp
                        run_prev = stamp
                    runs.append((run_start, run_prev + pd.Timedelta(minutes=1)))

                    repaired = []
                    for run_start, run_end in runs:
                        cursor = run_start
                        while cursor < run_end:
                            repair_end = min(cursor + pd.Timedelta(minutes=30), run_end)
                            repair_req = {
                                "symbol": symbol,
                                "resolution": str(resolution),
                                "date_format": "0",
                                "range_from": str(int(cursor.timestamp())),
                                "range_to": str(int(repair_end.timestamp())),
                            }
                            if "FUT" in str(symbol).upper():
                                repair_req["cont_flag"] = "1"
                            if oi_flag:
                                repair_req["oi_flag"] = "1"
                            try:
                                extra = _request(repair_req)
                            except Exception:
                                extra = []
                            if extra:
                                repaired.extend(extra)
                            cursor = repair_end
                            time.sleep(0.12)

                    if repaired:
                        df = pd.concat([df, _normalize(repaired)], ignore_index=True)
                        df = df[
                            (df["datetime"] >= session_start) &
                            (df["datetime"] < session_end)
                        ]
                        df = (
                            df.dropna(subset=["datetime", "open", "high", "low", "close"])
                              .sort_values("datetime")
                              .drop_duplicates(subset=["datetime"], keep="last")
                              .reset_index(drop=True)
                        )

        if df.empty:
            return pd.DataFrame()

        return df.drop(columns="timestamp", errors="ignore")

    def quotes(self, symbols):
        symbols = symbols if isinstance(symbols, str) else ",".join(symbols)
        return self._check(self.rest.quotes(data={"symbols": symbols}), "Quotes")

    def option_chain(self, underlying, strikecount=15, timestamp=None, greeks=True):
        data = {"symbol": underlying, "strikecount": int(strikecount), "greeks": "1" if greeks else "0"}
        if timestamp:
            data["timestamp"] = str(timestamp)
        # Current v3 SDK exposes optionchain(data=...).
        method = getattr(self.rest, "optionchain", None) or getattr(self.rest, "option_chain", None)
        if method is None:
            raise RuntimeError("Installed fyers-apiv3 SDK does not expose optionchain(). Upgrade fyers-apiv3.")
        return self._check(method(data=data), "Option chain")

    def choose_option(self, underlying, side, premium_min, premium_max, premium_target,
                      expiry_mode="Nearest", strikecount=25):
        """Select the long option contract: CE for bullish direction, PE for bearish direction."""
        first = self.option_chain(underlying, strikecount=strikecount, greeks=False)
        data = first.get("data", {})
        expiries = data.get("expiryData", []) or []
        if not expiries:
            raise RuntimeError("FYERS returned no option expiries for the underlying.")

        if expiry_mode == "Monthly":
            monthly = [x for x in expiries if x.get("expiry_flag") == "M"]
            expiry = monthly[0] if monthly else expiries[0]
        else:
            expiry = expiries[0]

        chain_resp = self.option_chain(
            underlying, strikecount=strikecount, timestamp=expiry.get("expiry"), greeks=False
        )
        chain = chain_resp.get("data", {}).get("optionsChain", []) or []
        wanted_type = "CE" if side == "BUY" else "PE"
        candidates = []
        for item in chain:
            if item.get("option_type") != wanted_type:
                continue
            try:
                ltp = float(item.get("ltp"))
            except (TypeError, ValueError):
                continue
            if premium_min <= ltp <= premium_max:
                candidates.append(item)

        if not candidates:
            raise RuntimeError(
                f"No {wanted_type} contract in premium range ₹{premium_min:.0f}-₹{premium_max:.0f}. "
                "Increase strikecount or widen the premium range."
            )

        chosen = min(candidates, key=lambda x: abs(float(x["ltp"]) - premium_target))
        return {
            "symbol": chosen.get("symbol"),
            "option_type": wanted_type,
            "strike": chosen.get("strike_price"),
            "ltp": float(chosen.get("ltp")),
            "bid": chosen.get("bid"),
            "ask": chosen.get("ask"),
            "expiry": expiry,
            "greeks": chosen.get("greeks", {}),
            "candidates": candidates,
        }

    def place_order(self, symbol, side, qty, order_type=2, product_type="INTRADAY",
                    limit_price=0, stop_price=0, stop_loss=0, take_profit=0,
                    dry_run=True, order_tag="VWAPBOT"):
        order = {
            "symbol": symbol, "qty": int(qty), "type": int(order_type), "side": int(side),
            "productType": product_type, "limitPrice": float(limit_price),
            "stopPrice": float(stop_price), "disclosedQty": 0, "validity": "DAY",
            "offlineOrder": False, "stopLoss": float(stop_loss),
            "takeProfit": float(take_profit), "orderTag": order_tag,
            "isSliceOrder": False,
        }
        if dry_run:
            return {"s": "dry_run", "order": order}
        return self._check(self.rest.place_order(data=order), "Place order")

    def place_market_order(self, symbol, side, qty, product_type="INTRADAY", dry_run=True,
                           order_tag="VWAPBOT"):
        return self.place_order(symbol, side, qty, 2, product_type, dry_run=dry_run, order_tag=order_tag)

    def place_long_option_entry(self, symbol, qty, product_type="INTRADAY",
                                stop_loss=0, take_profit=0, dry_run=True,
                                order_tag="VWAPBOT"):
        """Place an option ENTRY. This method is intentionally BUY-only."""
        return self.place_order(
            symbol=symbol, side=1, qty=qty, order_type=2,
            product_type=product_type, stop_loss=stop_loss,
            take_profit=take_profit, dry_run=dry_run, order_tag=order_tag,
        )

    def start_data_socket(
        self, symbols, on_message, on_error=None, on_close=None, on_connect=None,
        lite_mode=False, data_type="SymbolUpdate", queue_interval_ms=50,
        restart_event=None,
    ):
        """Start one FYERS market-data socket and let the SDK own reconnects.

        The FYERS v3 SDK's supported pattern is a single FyersDataSocket with
        reconnect=True.  The application must not create a second socket from a
        watchdog while the SDK is already reconnecting; doing so can leave the
        SDK's internal message-queue worker racing teardown and produce the
        ``message_thread_stop_event is None`` / ``Connection is already closed``
        failures seen in Streamlit.

        ``restart_event`` is retained for API compatibility but is intentionally
        ignored.  A stale feed is repaired by the engine from REST history while
        the same socket continues its SDK-managed reconnect cycle.
        """
        if not data_ws:
            raise RuntimeError("Install fyers-apiv3")
        symbols = list(dict.fromkeys(
            str(x).strip() for x in (symbols or []) if str(x).strip()
        ))
        if not symbols:
            raise ValueError("At least one symbol is required for the market-data socket.")

        sock = None

        def _connect():
            # Subscribe only after the SDK has authenticated.  FYERS documents
            # this callback as the place to resubscribe after every reconnect.
            if on_connect:
                on_connect(sock)
            else:
                sock.subscribe(symbols=symbols, data_type=data_type)
            try:
                sock.setQueueProcessInterval(int(queue_interval_ms))
            except (AttributeError, TypeError, ValueError):
                pass
            # This is the official SDK pattern: keep_running() belongs to the
            # on_connect callback and keeps this single socket alive.
            sock.keep_running()

        sock = data_ws.FyersDataSocket(
            access_token=f"{self.app_id}:{self.access_token}",
            log_path="",
            litemode=bool(lite_mode),
            write_to_file=False,
            reconnect=True,
            reconnect_retry=50,
            on_connect=_connect,
            on_close=on_close or (lambda msg: None),
            on_error=on_error or (lambda msg: None),
            on_message=on_message,
        )
        sock.connect()
        return sock

    @staticmethod
    def subscribe_data_socket(sock, symbols, data_type="SymbolUpdate"):
        if sock is None:
            raise RuntimeError("Market-data socket is not connected.")
        symbols = list(dict.fromkeys([str(x).strip() for x in (symbols or []) if str(x).strip()]))
        if symbols:
            return sock.subscribe(symbols=symbols, data_type=data_type)
        return None

    @staticmethod
    def unsubscribe_data_socket(sock, symbols, data_type="SymbolUpdate"):
        if sock is None:
            return None
        symbols = list(dict.fromkeys([str(x).strip() for x in (symbols or []) if str(x).strip()]))
        if symbols:
            return sock.unsubscribe(symbols=symbols, data_type=data_type)
        return None


    def start_order_socket(self, on_orders=None, on_trades=None, on_positions=None, on_general=None):
        """Start the dedicated FYERS v3 Order WebSocket.

        FYERS documents order websocket as a separate socket from market data;
        it delivers order, trade and position changes without REST polling.
        """
        if order_ws is None or not hasattr(order_ws, "FyersOrderSocket"):
            raise RuntimeError(
                "FYERS order WebSocket support is unavailable in this SDK build; "
                "market-data WebSocket remains available."
            )
        callbacks = dict(
            on_orders=on_orders or (lambda msg: None),
            on_trades=on_trades or (lambda msg: None),
            on_positions=on_positions or (lambda msg: None),
            on_general=on_general or (lambda msg: None),
        )

        # FYERS' current v3 sample uses connect() followed by subscribe().
        # Keep the order socket isolated from the market-data socket so a
        # missing/unsupported order feed can never stop live chart ticks.
        sock = order_ws.FyersOrderSocket(
            access_token=f"{self.app_id}:{self.access_token}",
            write_to_file=False,
            log_path="",
            on_orders=callbacks["on_orders"],
            on_trades=callbacks["on_trades"],
            on_positions=callbacks["on_positions"],
            on_general=callbacks["on_general"],
        )
        sock.connect()
        sock.subscribe(data_type="OnOrders,OnTrades,OnPositions,OnGeneral")
        return sock
