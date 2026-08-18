from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

try:
    from fyers_apiv3 import fyersModel
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    fyersModel = None
    data_ws = None


class FyersClient:
    """Small wrapper around FYERS API v3 REST + market-data WebSocket."""

    def __init__(self, app_id: str, access_token: str):
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3: pip install fyers-apiv3")
        self.app_id = (app_id or "").strip()
        self.access_token = (access_token or "").strip()
        if not self.app_id or not self.access_token:
            raise ValueError("App ID and access token are required")

        # REST SDK expects the raw access token.
        self.rest = fyersModel.FyersModel(
            client_id=self.app_id,
            token=self.access_token,
            is_async=False,
            log_path="",
        )

    @staticmethod
    def auth_url(app_id: str, secret_id: str, redirect_uri: str, state: str = "fyers_vwap"):
        """Generate the FYERS v3 OAuth authorization URL."""
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3")
        session = fyersModel.SessionModel(
            client_id=app_id.strip(),
            secret_key=secret_id.strip(),
            redirect_uri=redirect_uri.strip(),
            response_type="code",
            state=state,
            grant_type="authorization_code",
        )
        return session.generate_authcode()

    @staticmethod
    def exchange_auth_code(app_id: str, secret_id: str, redirect_uri: str, auth_code: str):
        """Exchange a one-time auth_code for a daily access token."""
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3")
        session = fyersModel.SessionModel(
            client_id=app_id.strip(),
            secret_key=secret_id.strip(),
            redirect_uri=redirect_uri.strip(),
            response_type="code",
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
                    "Generate a fresh v3 access token and make sure the App ID matches the token."
                )
            raise RuntimeError(f"{name}: {response}")
        return response

    def profile(self):
        return self._check(self.rest.get_profile(), "Profile")

    def funds(self):
        return self._check(self.rest.funds(), "Funds")

    def positions(self):
        return self._check(self.rest.positions(), "Positions")

    def holdings(self):
        return self._check(self.rest.holdings(), "Holdings")

    def orders(self):
        return self._check(self.rest.orderbook(), "Orderbook")

    def trades(self):
        return self._check(self.rest.tradebook(), "Tradebook")

    def history(self, symbol: str, resolution: str, days: int = 10) -> pd.DataFrame:
        end = datetime.now(IST).date()
        start = end - timedelta(days=days)
        data = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",
        }
        resp = self._check(self.rest.history(data=data), "History")
        candles = resp.get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        return df.drop(columns="timestamp").sort_values("datetime").reset_index(drop=True)

    def place_market_order(self, symbol: str, side: int, qty: int,
                           product_type: str = "INTRADAY", dry_run: bool = True,
                           order_tag: str = "VWAPBOT"):
        order = {
            "symbol": symbol,
            "qty": int(qty),
            "type": 2,
            "side": int(side),
            "productType": product_type,
            "limitPrice": 0,
            "stopPrice": 0,
            "validity": "DAY",
            "offlineOrder": False,
            "disclosedQty": 0,
            "orderTag": order_tag,
        }
        if dry_run:
            return {"s": "dry_run", "order": order}
        return self._check(self.rest.place_order(data=order), "Place order")

    def start_data_socket(self, symbols, on_message, on_error=None, on_close=None, on_connect=None):
        if not data_ws:
            raise RuntimeError("Install fyers-apiv3")

        def _connect():
            if on_connect:
                on_connect(sock)
            sock.subscribe(symbols=symbols, data_type="SymbolUpdate")
            sock.keep_running()

        # FYERS WebSocket expects app_id:access_token.
        sock = data_ws.FyersDataSocket(
            access_token=f"{self.app_id}:{self.access_token}",
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=_connect,
            on_close=on_close or (lambda msg: None),
            on_error=on_error or (lambda msg: None),
            on_message=on_message,
        )
        sock.connect()
        return sock
