from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

try:
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    fyersModel = None
    data_ws = None


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

    def history(self, symbol, resolution, days=31):
        end = datetime.now(IST).date()
        start = end - timedelta(days=days)
        data = {
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1",
        }
        resp = self._check(self.rest.history(data=data), "History")
        # FYERS SDK versions have returned both top-level and nested data
        # payloads. Accept either shape so historical charts/backtests do not
        # silently render an empty dataset.
        payload = resp.get("data", resp)
        if isinstance(payload, dict):
            candles = payload.get("candles", [])
        else:
            candles = resp.get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["datetime","open","high","low","close","volume"])
        df = pd.DataFrame(candles, columns=["timestamp","open","high","low","close","volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        return df.drop(columns="timestamp").sort_values("datetime").reset_index(drop=True)

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
        """Select CE for BUY signal and PE for SELL signal by option LTP in the requested band."""
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

    def start_data_socket(
        self, symbols, on_message, on_error=None, on_close=None, on_connect=None,
        lite_mode=False, data_type="SymbolUpdate", queue_interval_ms=50,
    ):
        """Create exactly one long-lived FYERS DataSocket.

        FYERS' SDK owns transient reconnects when reconnect=True.  The app does
        not run a second reconnect loop around this socket.  Subscriptions are
        restored from the callback after every authenticated connection.

        Full SymbolUpdate is the default because the VWAP engine can use
        vol_traded_today when FYERS supplies it.  Set lite_mode=True only when
        LTP-only streaming is explicitly desired.
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
            # Subscribe only after the SDK has authenticated.
            sock.subscribe(symbols=symbols, data_type=data_type)
            # The SDK exposes queue processing control in current v3 builds.
            try:
                sock.setQueueProcessInterval(int(queue_interval_ms))
            except (AttributeError, TypeError, ValueError):
                pass
            if on_connect:
                on_connect(sock)
            sock.keep_running()

        sock = data_ws.FyersDataSocket(
            access_token=f"{self.app_id}:{self.access_token}",
            log_path="",
            litemode=bool(lite_mode),
            write_to_file=False,
            reconnect=True,
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
