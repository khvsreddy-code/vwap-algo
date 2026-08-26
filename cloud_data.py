"""Cloud market-data recorder for FYERS VWAP Trader.

Primary storage is Supabase/Postgres. No local database or local market-data files
are used. The recorder is deliberately decoupled from the Streamlit UI and the
FYERS websocket callback: ticks are accumulated in memory, completed 1-minute
candles are queued, and a background worker retries cloud writes until they
succeed or the process is stopped.
"""
from __future__ import annotations

import io
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

IST = "Asia/Kolkata"

try:  # Optional until Supabase is configured/installed.
    from supabase import Client, create_client
except Exception:  # pragma: no cover - exercised on machines without the package.
    Client = Any  # type: ignore
    create_client = None


def _as_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _tick_time(message: dict) -> datetime:
    raw = message.get("last_traded_time") or message.get("timestamp") or message.get("time")
    try:
        ts = float(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo(IST))
    except (TypeError, ValueError, OverflowError):
        return datetime.now(ZoneInfo(IST))


def _minute_start(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


class CloudMarketStore:
    """Small, retrying Supabase/Postgres adapter.

    The Streamlit process uses a server-side Supabase secret key. It must never
    be sent to browser code. Supabase documents secret keys as backend-only and
    says they bypass RLS, so this adapter is intended for the Streamlit server.
    """

    def __init__(self, url: str, key: str, timeout: float = 10.0):
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_SECRET_KEY are required.")
        if create_client is None:
            raise RuntimeError("Missing dependency: install the 'supabase' Python package.")
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = float(timeout)
        self.client: Client = create_client(self.url, self.key)

    @classmethod
    def from_env(cls) -> "CloudMarketStore":
        url = os.getenv("SUPABASE_URL", "").strip()
        key = (
            os.getenv("SUPABASE_SECRET_KEY", "").strip()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        return cls(url, key)

    def health_check(self) -> dict:
        started = time.monotonic()
        response = (
            self.client.table("market_candles_1m")
            .select("symbol", count="exact")
            .limit(1)
            .execute()
        )
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {"ok": True, "rows": int(response.count or 0), "latency_ms": elapsed_ms}

    def fetch_instruments(self, underlying: Optional[str] = None) -> List[dict]:
        q = self.client.table("instruments").select("*").order("symbol")
        if underlying:
            q = q.eq("underlying", underlying)
        response = q.execute()
        return response.data or []

    def upsert_instruments(self, rows: List[dict], retries: int = 5) -> None:
        if not rows:
            return
        last = None
        for attempt in range(retries):
            try:
                self.client.table("instruments").upsert(
                    rows, on_conflict="symbol", ignore_duplicates=False
                ).execute()
                return
            except Exception as exc:
                last = exc
                if attempt + 1 < retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        raise RuntimeError(f"Supabase instrument write failed after {retries} attempts: {last}")

    def upsert_candles(self, rows: List[dict], retries: int = 6, chunk_size: int = 500) -> int:
        if not rows:
            return 0
        written = 0
        for offset in range(0, len(rows), chunk_size):
            chunk = rows[offset:offset + chunk_size]
            last = None
            for attempt in range(retries):
                try:
                    self.client.table("market_candles_1m").upsert(
                        chunk,
                        on_conflict="symbol,candle_start",
                        ignore_duplicates=False,
                    ).execute()
                    written += len(chunk)
                    last = None
                    break
                except Exception as exc:
                    last = exc
                    if attempt + 1 < retries:
                        time.sleep(min(10.0, 0.75 * (2 ** attempt)))
            if last is not None:
                raise RuntimeError(
                    f"Supabase candle write failed after {retries} attempts "
                    f"for batch starting at row {offset}: {last}"
                )
        return written

    def fetch_candles(self, start: datetime, end: datetime, symbols: Optional[List[str]] = None, page_size: int = 1000) -> pd.DataFrame:
        rows = []
        offset = 0
        while True:
            q = (
                self.client.table("market_candles_1m")
                .select("*")
                .gte("candle_start", start.isoformat())
                .lt("candle_start", end.isoformat())
                .order("candle_start")
                .range(offset, offset + page_size - 1)
            )
            if symbols:
                q = q.in_("symbol", symbols)
            response = q.execute()
            batch = response.data or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return pd.DataFrame(rows)

    def fetch_existing_candle_keys(self, start: datetime, end: datetime, symbol: str, page_size: int = 1000) -> set[tuple[str, str]]:
        """Return existing primary keys for one symbol/day.

        This is intentionally separate from upsert: the end-of-day recovery
        path uses it to avoid sending already-persisted candles back to
        Supabase. Existing rows are therefore neither updated nor rewritten.
        """
        keys: set[tuple[str, str]] = set()
        offset = 0
        while True:
            response = (
                self.client.table("market_candles_1m")
                .select("symbol,candle_start")
                .eq("symbol", symbol)
                .gte("candle_start", start.isoformat())
                .lt("candle_start", end.isoformat())
                .order("candle_start")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            batch = response.data or []
            for row in batch:
                if row.get("symbol") and row.get("candle_start"):
                    keys.add((str(row["symbol"]), str(row["candle_start"])))
            if len(batch) < page_size:
                break
            offset += page_size
        return keys

    def insert_missing_candles(self, rows: List[dict], retries: int = 5, chunk_size: int = 500) -> dict:
        """Insert only candle keys that do not already exist.

        The normal live recorder may use upsert because a currently forming
        candle can be revised before it closes. End-of-day recovery is
        different: it must be additive/idempotent and must not rewrite data
        that is already in the database. A per-symbol key check makes that
        guarantee explicit. The final insert also ignores a rare race where
        another writer creates the same key between the check and insert.
        """
        if not rows:
            return {"inserted": 0, "skipped": 0, "failed": 0}

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            symbol = str(row.get("symbol") or "").strip()
            if symbol:
                grouped[symbol].append(row)

        inserted = 0
        skipped = 0
        inserted_rows: list[dict] = []
        for symbol, symbol_rows in grouped.items():
            # Derive the narrowest day/window from the rows rather than doing
            # one huge database scan.
            stamps = [pd.Timestamp(r["candle_start"]) for r in symbol_rows if r.get("candle_start")]
            if not stamps:
                continue
            start = min(stamps).to_pydatetime()
            end = (max(stamps) + pd.Timedelta(minutes=1)).to_pydatetime()
            existing = self.fetch_existing_candle_keys(start, end, symbol)
            missing = [
                r for r in symbol_rows
                if (symbol, str(r.get("candle_start"))) not in existing
            ]
            skipped += len(symbol_rows) - len(missing)
            for offset in range(0, len(missing), chunk_size):
                chunk = missing[offset:offset + chunk_size]
                last = None
                for attempt in range(retries):
                    try:
                        # ignore_duplicates protects against a concurrent live
                        # writer inserting a key after our existence check.
                        response = (
                            self.client.table("market_candles_1m")
                            .upsert(chunk, on_conflict="symbol,candle_start", ignore_duplicates=True)
                            .execute()
                        )
                        # Supabase may return the inserted rows, but we do not
                        # rely on that response shape; every key in this chunk
                        # was missing at check time.
                        inserted += len(chunk)
                        inserted_rows.extend(chunk)
                        last = None
                        break
                    except Exception as exc:
                        last = exc
                        if attempt + 1 < retries:
                            time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                if last is not None:
                    raise RuntimeError(
                        f"Supabase missing-candle insert failed for {symbol}: {last}"
                    )
        return {"inserted": inserted, "skipped": skipped, "failed": 0, "rows": inserted_rows}



class CloudCandleRecorder:
    """Accumulate FYERS ticks into 1-minute candles and persist them to Supabase."""

    def __init__(self, store: CloudMarketStore, instrument_meta: Dict[str, dict]):
        self.store = store
        self.instrument_meta = dict(instrument_meta)
        # ``instrument_meta`` is the historical registry. ``_active_symbols`` is
        # the live universe currently accepted from the websocket. Keeping retired
        # metadata lets an in-progress candle finish cleanly after a universe roll
        # without accepting any new ticks for the retired contract.
        self._active_symbols = set(self.instrument_meta)
        self._lock = threading.RLock()
        self._candles: Dict[str, dict] = {}
        self._cum_volume: Dict[str, int] = {}
        self._oi: Dict[str, dict] = {}
        self._pending: deque[dict] = deque()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._writer_loop, name="supabase-market-writer", daemon=True)
        self._started = False
        self.last_write_at = None
        self.last_write_error = None
        self.last_write_count = 0
        self.total_written = 0
        self.pending_count = 0

    def start(self):
        if self._started:
            return
        self.store.upsert_instruments(list(self.instrument_meta.values()))
        self._stop.clear()
        self._worker.start()
        self._started = True

    def stop(self, flush=True):
        if flush:
            self.flush_all()
        self._stop.set()
        if self._worker.is_alive():
            self._worker.join(timeout=12)
        self._started = False

    def register_instruments(self, rows: Dict[str, dict]):
        if not rows:
            return
        with self._lock:
            self.instrument_meta.update(rows)
            self._active_symbols.update(rows)
        self.store.upsert_instruments(list(rows.values()))

    def deactivate_instruments(self, symbols: Iterable[str]):
        """Stop accepting new ticks for retired contracts without deleting metadata.

        Existing in-progress candles remain in memory until their normal minute
        boundary (or a final flush), so a universe roll cannot silently discard
        the last observed prices of a contract that just left the 40-symbol set.
        Historical rows in Supabase are never deleted.
        """
        with self._lock:
            self._active_symbols.difference_update(str(s).strip() for s in symbols if str(s).strip())

    def active_symbols(self) -> set:
        with self._lock:
            return set(self._active_symbols)

    def set_oi_snapshot(self, symbol: str, oi=None, oi_change=None, prev_oi=None, timestamp=None):
        if symbol not in self.instrument_meta:
            return
        ts = timestamp or datetime.now(ZoneInfo(IST))
        with self._lock:
            self._oi[symbol] = {
                "timestamp": ts,
                "oi": _as_int(oi),
                "oi_change": _as_int(oi_change),
                "prev_oi": _as_int(prev_oi),
            }

    def on_tick(self, message: dict):
        if not isinstance(message, dict):
            return
        symbol = str(message.get("symbol") or "").strip()
        with self._lock:
            if symbol not in self._active_symbols:
                return
        ltp = _as_float(message.get("ltp"))
        if ltp is None or ltp <= 0:
            return
        ts = _tick_time(message)
        start = _minute_start(ts)
        with self._lock:
            current = self._candles.get(symbol)
            if current is not None and start > current["candle_start"]:
                self._pending.append(self._finalize_locked(symbol, current))
                current = None
            if current is None:
                current = {
                    "symbol": symbol,
                    "candle_start": start,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": 0,
                }
                self._candles[symbol] = current
            else:
                current["high"] = max(float(current["high"]), ltp)
                current["low"] = min(float(current["low"]), ltp)
                current["close"] = ltp

            cum_vol = _as_int(message.get("vol_traded_today"))
            if cum_vol is not None:
                previous = self._cum_volume.get(symbol)
                if previous is not None:
                    current["volume"] += max(0, cum_vol - previous)
                self._cum_volume[symbol] = cum_vol

            # Some FYERS payloads can contain OI fields. Keep them when present;
            # the option-chain snapshotter can overwrite them with authoritative
            # OI/OI-change values later.
            if message.get("oi") is not None:
                self._oi[symbol] = {
                    "timestamp": ts,
                    "oi": _as_int(message.get("oi")),
                    "oi_change": _as_int(message.get("oich") or message.get("oi_change")),
                    "prev_oi": _as_int(message.get("prev_oi")),
                }
            self.pending_count = len(self._pending)

    def flush_all(self):
        with self._lock:
            for symbol, candle in list(self._candles.items()):
                self._pending.append(self._finalize_locked(symbol, candle))
            self._candles.clear()
            rows = list(self._pending)
            self._pending.clear()
            self.pending_count = 0
        if rows:
            self._write(rows)

    def flush_completed(self):
        now_minute = _minute_start(datetime.now(ZoneInfo(IST)))
        with self._lock:
            for symbol, candle in list(self._candles.items()):
                if candle["candle_start"] < now_minute:
                    self._pending.append(self._finalize_locked(symbol, candle))
                    del self._candles[symbol]
            rows = list(self._pending)
            self._pending.clear()
            self.pending_count = 0
        if rows:
            self._write(rows)

    def _finalize_locked(self, symbol: str, candle: dict) -> dict:
        meta = self.instrument_meta[symbol]
        oi = self._oi.get(symbol, {})
        row = {
            **meta,
            "candle_start": candle["candle_start"].isoformat(),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": int(candle.get("volume") or 0),
            "ltp": float(candle["close"]),
            "oi": oi.get("oi"),
            "oi_change": oi.get("oi_change"),
            "prev_oi": oi.get("prev_oi"),
            "oi_snapshot_at": oi.get("timestamp").isoformat() if oi.get("timestamp") else None,
            "source": "fyers_websocket",
        }
        return row

    def _write(self, rows: List[dict]):
        try:
            count = self.store.upsert_candles(rows)
            self.last_write_at = datetime.now(ZoneInfo(IST))
            self.last_write_count = count
            self.total_written += count
            self.last_write_error = None
        except Exception as exc:
            self.last_write_error = str(exc)
            # Put rows back at the front so transient Supabase/network failures
            # do not silently lose market data.
            with self._lock:
                for row in reversed(rows):
                    self._pending.appendleft(row)
                self.pending_count = len(self._pending)

    def _writer_loop(self):
        while not self._stop.wait(1.0):
            self.flush_completed()
            # Retry queued failed writes even if no new ticks arrive.
            with self._lock:
                if not self._pending:
                    continue
                rows = list(self._pending)
                self._pending.clear()
                self.pending_count = 0
            self._write(rows)


def option_chain_rows(response: dict, underlying: str, expiry: dict, wanted: Optional[set] = None) -> Dict[str, dict]:
    """Normalize FYERS option-chain contracts into recorder instrument rows."""
    data = response.get("data", response) if isinstance(response, dict) else {}
    chain = data.get("optionsChain", []) or []
    out = {}
    expiry_value = expiry.get("expiry") if isinstance(expiry, dict) else expiry
    for item in chain:
        symbol = str(item.get("symbol") or "").strip()
        option_type = str(item.get("option_type") or "").upper()
        if not symbol or option_type not in {"CE", "PE"}:
            continue
        if wanted and symbol not in wanted:
            continue
        strike = _as_float(item.get("strike_price"))
        out[symbol] = {
            "symbol": symbol,
            "underlying": underlying,
            "expiry": str(expiry_value) if expiry_value is not None else None,
            "strike": strike,
            "option_type": option_type,
        }
    return out
