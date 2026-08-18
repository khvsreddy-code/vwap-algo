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
    def __init__(self, app_id: str, access_token: str):
        if not fyersModel:
            raise RuntimeError("Install fyers-apiv3")
        self.app_id = app_id.strip()
        self.access_token = access_token.strip()
        if not self.app_id or not self.access_token:
            raise ValueError("App ID and access token are required")
        self.rest = fyersModel.FyersModel(
            client_id=self.app_id,
            token=self.access_token,
            is_async=False,
            log_path="",
        )

    def profile(self):
        return self.rest.get_profile()

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
        resp = self.rest.history(data=data)
        if resp.get("s") != "ok":
            raise RuntimeError(str(resp))
        candles = resp.get("candles", [])
        if not candles:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        return df.drop(columns="timestamp").sort_values("datetime").reset_index(drop=True)

    def place_market_order(self, symbol: str, side: int, qty: int, product_type: str = "INTRADAY", dry_run: bool = True):
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
        }
        if dry_run:
            return {"s": "dry_run", "order": order}
        return self.rest.place_order(data=order)

    def start_data_socket(self, symbols, on_message, on_error=None, on_close=None, on_connect=None):
        if not data_ws:
            raise RuntimeError("Install fyers-apiv3")

        def _connect():
            if on_connect:
                on_connect(sock)
            sock.subscribe(symbols=symbols, data_type="SymbolUpdate")
            sock.keep_running()

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
