"""Read-only historical Alpaca SIP data with validated, isolated caches.

Minute bars are raw/as traded. Alpaca's ``asof`` identifies a ticker; it does
not limit split adjustments to a historical date. To put a prior close on the
requested day's basis, divide its split adjustment by the adjustment of a
matched target-day minute. Future splits cancel. The calibration responses
are cached together so responses fetched before/after later splits cannot mix.

API references: https://docs.alpaca.markets/us/reference/stockbars and
https://docs.alpaca.markets/us/v1.1/docs/market-data-faq.
Alpaca's price rounding can affect the derived factor; visibly inconsistent
raw/adjusted reference prices are rejected rather than guessed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, time as clock, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import DataConfig
from .models import Bar, DataError, PreviousClose


EASTERN = ZoneInfo("America/New_York")
API_BASE = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
CACHE_VERSION = 1
T = TypeVar("T")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _symbol(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", value):
        raise DataError("Invalid stock ticker")
    return value


def _number(value: Any, field: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"Alpaca returned a nonnumeric {field}")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise DataError(f"Alpaca returned an invalid {field}") from None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise DataError(f"Alpaca returned an invalid {field}")
    return number


class AlpacaClient:
    def __init__(self, config: DataConfig, api_key: str | None, secret_key: str | None,
                 *, offline: bool = False, refresh_cache: bool = False) -> None:
        if offline and refresh_cache:
            raise DataError("Offline mode cannot refresh the Alpaca cache")
        for credential in (api_key, secret_key):
            if credential and any(ord(char) <= 32 or ord(char) >= 127 for char in credential):
                raise DataError("Invalid Alpaca credential format")
        self.config = config
        self._api_key = api_key or None
        self._secret_key = secret_key or None
        self.offline = offline
        self.refresh_cache = refresh_cache
        self._opener = build_opener(_NoRedirect())
        self._last_request: float | None = None
        self._sessions: dict[date, date] = {}

    @staticmethod
    def _bar_params(symbol: str, first: date, last: date, *, minute: bool,
                    asof: date, adjustment: str = "raw") -> dict[str, str]:
        start = datetime.combine(first, clock.min, EASTERN)
        end = datetime.combine(last + timedelta(days=1), clock.min, EASTERN) - timedelta(microseconds=1)
        return {"symbols": symbol, "timeframe": "1Min" if minute else "1Day",
                "start": start.isoformat(), "end": end.isoformat(),
                "feed": "sip", "adjustment": adjustment, "asof": asof.isoformat(),
                "sort": "asc", "limit": "10000"}

    def minute_cache_path(self, symbol: str, day: date) -> Path:
        params = self._bar_params(_symbol(symbol), day, day, minute=True, asof=day)
        return self._cache_path(BARS_PATH, params)

    def minute_bars(self, symbol: str, day: date) -> list[Bar]:
        symbol = _symbol(symbol)
        params = self._bar_params(symbol, day, day, minute=True, asof=day)
        return self._cached(BARS_PATH, params,
                            lambda pages: self._decode_bars(pages, symbol, day, day, minute=True),
                            cacheable=day < datetime.now(EASTERN).date())

    def _previous_session(self, day: date) -> date:
        if day in self._sessions:
            return self._sessions[day]
        first = day - timedelta(days=self.config.previous_close_lookback_days)
        last = day - timedelta(days=1)
        symbol = _symbol(self.config.calendar_symbol)
        params = self._bar_params(symbol, first, last, minute=False, asof=day)
        bars = self._cached(BARS_PATH, params,
                            lambda pages: self._decode_bars(pages, symbol, first, last, minute=False))
        if not bars:
            raise DataError(f"No previous market session found for {day} using {symbol}")
        previous_day = max(bar.timestamp.date() for bar in bars)
        self._sessions[day] = previous_day
        return previous_day

    def previous_close(self, symbol: str, day: date) -> PreviousClose:
        symbol = _symbol(symbol)
        previous_day = self._previous_session(day)

        def fetch() -> list[dict[str, Any]]:
            # These four responses form one calibration. Never load separately
            # cached split-adjusted responses: later splits change their basis.
            prior_params = self._bar_params(symbol, previous_day, previous_day,
                                            minute=False, asof=day)
            raw_prior = self._request_pages(BARS_PATH, prior_params)
            adjusted_prior = self._request_pages(BARS_PATH, {**prior_params, "adjustment": "split"})
            raw_bars = self.minute_bars(symbol, day)
            if not raw_bars:
                raise DataError(f"No target-day SIP bar for {symbol} on {day}; cannot normalize prior close")
            reference = raw_bars[0]
            params = self._bar_params(symbol, day, day, minute=True, asof=day, adjustment="split")
            # Only a matching minute is needed. Its market price is divided out;
            # it is not used to infer a gap or forecast a future price.
            params.update(start=reference.timestamp.isoformat(),
                          end=(reference.timestamp + timedelta(minutes=1) - timedelta(microseconds=1)).isoformat())
            adjusted_reference = self._request_pages(BARS_PATH, params)
            raw_reference = [{"bars": {symbol: [{"t": reference.timestamp.isoformat(),
                "o": reference.open, "h": reference.high, "l": reference.low,
                "c": reference.close, "v": reference.volume}]}}]
            return [{"raw_prior": raw_prior, "adjusted_prior": adjusted_prior,
                     "raw_reference": raw_reference, "adjusted_reference": adjusted_reference}]

        def decode(pages: list[dict[str, Any]]) -> PreviousClose:
            if len(pages) != 1:
                raise DataError("Invalid Alpaca prior-close calibration")
            bundle = pages[0]
            values: dict[str, Bar] = {}
            for key in ("raw_prior", "adjusted_prior", "raw_reference", "adjusted_reference"):
                minute = key.endswith("reference")
                target = day if minute else previous_day
                self._validate_pages(bundle.get(key))
                bars = self._decode_bars(bundle[key], symbol, target, target, minute=minute)
                if len(bars) != 1:
                    raise DataError(f"Missing or ambiguous Alpaca {key.replace('_', ' ')} for {symbol} on {target}")
                values[key] = bars[0]
            raw = values["raw_reference"]
            adjusted = values["adjusted_reference"]
            if raw.timestamp != adjusted.timestamp:
                raise DataError("Alpaca split calibration timestamps do not match")
            future_factor = _number(adjusted.high / raw.high, "target-date split adjustment")
            # Use the largest reference price to reduce adjustment-rounding error.
            # All four prices must be consistent with the same adjustment factor.
            for field in ("open", "high", "low", "close"):
                if not math.isclose(getattr(adjusted, field), getattr(raw, field) * future_factor,
                                    rel_tol=1e-5, abs_tol=1e-6):
                    raise DataError("Alpaca raw/split reference bars are inconsistent; use --refresh-cache")
            source_close = values["raw_prior"].close
            close = _number(values["adjusted_prior"].close / future_factor, "normalized prior close")
            factor = _number(close / source_close, "split factor")
            if math.isclose(factor, 1.0, rel_tol=1e-9):
                # Avoid float noise making an exactly-threshold gap qualify.
                close, factor = source_close, 1.0
            return PreviousClose(previous_day, close, source_close, factor)

        return self._cached("/backtest/previous-close", {"symbol": symbol, "day": str(day),
                            "previous_day": str(previous_day), "feed": "sip", "normalization": "paired-split-v1"},
                            decode, fetch=fetch, cacheable=day < datetime.now(EASTERN).date())

    @staticmethod
    def _decode_bars(pages: list[dict[str, Any]], symbol: str, first: date, last: date,
                     *, minute: bool) -> list[Bar]:
        bars: list[Bar] = []
        seen: set[datetime | date] = set()
        for page in pages:
            if "bars" not in page:
                raise DataError("Alpaca bars response is incomplete")
            groups = page["bars"]
            if groups is None:
                groups = {}
            if not isinstance(groups, dict) or any(key != symbol for key in groups):
                raise DataError("Alpaca returned unexpected symbols or invalid bars")
            rows = groups.get(symbol, [])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise DataError("Alpaca returned invalid bar rows")
            for row in rows:
                try:
                    stamp = row.get("t")
                    if not isinstance(stamp, str):
                        raise ValueError
                    timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if timestamp.tzinfo is None or timestamp.second or timestamp.microsecond:
                        raise ValueError
                    timestamp = timestamp.astimezone(EASTERN)
                except (ValueError, OverflowError):
                    raise DataError("Alpaca returned an invalid bar timestamp") from None
                if not first <= timestamp.date() <= last:
                    raise DataError("Alpaca returned a bar outside the requested Eastern dates")
                if not minute and timestamp.time() != clock.min:
                    raise DataError("Alpaca daily bar timestamp is not Eastern midnight")
                identity = timestamp if minute else timestamp.date()
                if identity in seen:
                    raise DataError("Alpaca returned duplicate bar timestamps")
                seen.add(identity)
                opening, high, low, close = (_number(row.get(key), key) for key in ("o", "h", "l", "c"))
                if not low <= min(opening, close) <= max(opening, close) <= high:
                    raise DataError("Alpaca returned inconsistent OHLC prices")
                bars.append(Bar(timestamp, opening, high, low, close,
                                _number(row.get("v", 0), "volume", positive=False)))
        return sorted(bars, key=lambda bar: bar.timestamp)

    def _cache_path(self, path: str, params: dict[str, str]) -> Path:
        identity = {"base_url": API_BASE, "path": path, "params": params}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return Path(self.config.cache_dir) / "alpaca_sip" / f"{digest}.json"

    def _cached(self, path: str, params: dict[str, str], decode: Callable[[list[dict[str, Any]]], T],
                *, cacheable: bool = True, fetch: Callable[[], list[dict[str, Any]]] | None = None) -> T:
        fetch = fetch or (lambda: self._request_pages(path, params))
        if not cacheable:
            if self.offline:
                raise DataError("Offline Alpaca data requires a completed historical Eastern date")
            return decode(fetch())
        identity = {"base_url": API_BASE, "path": path, "params": params}
        cache_file = self._cache_path(path, params)
        if not self.refresh_cache and cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if (not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION
                        or payload.get("request") != identity or payload.get("complete") is not True):
                    raise DataError("Alpaca cache identity/completeness check failed")
                pages = payload.get("pages")
                self._validate_pages(pages)
                return decode(pages)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                if isinstance(exc, DataError):
                    raise DataError(f"Invalid Alpaca cache; use --refresh-cache: {exc}") from None
                raise DataError("Unreadable Alpaca cache; use --refresh-cache") from None
        if self.offline:
            raise DataError(f"Offline Alpaca cache miss: {path}")
        pages = fetch()
        self._validate_pages(pages)
        result = decode(pages)
        self._write_cache(cache_file, {"version": CACHE_VERSION, "request": identity,
                                      "complete": True, "pages": pages})
        return result

    @staticmethod
    def _validate_pages(pages: Any) -> None:
        if not isinstance(pages, list) or not pages:
            raise DataError("Alpaca response has no complete pages")
        for page in pages:
            if not isinstance(page, dict) or "code" in page or "message" in page:
                raise DataError("Alpaca API returned an invalid or error response")
            if "next_page_token" in page:
                raise DataError("Alpaca cache contains unfinished pagination")

    def _request_pages(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        if path != BARS_PATH:
            raise DataError("Unsupported Alpaca data endpoint")
        query = dict(params)
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        while True:
            page = self._request_json(f"{API_BASE}{path}?{urlencode(sorted(query.items()))}")
            if not isinstance(page, dict) or "code" in page or "message" in page:
                raise DataError("Alpaca API returned an invalid or error response")
            token = page.pop("next_page_token", None)
            if token is not None and (not isinstance(token, str) or not token or token in seen or len(seen) >= 10000):
                raise DataError("Alpaca returned an invalid/repeating pagination token")
            pages.append(page)
            if token is None:
                self._validate_pages(pages)
                return pages
            seen.add(token)
            # Tokens remain query values on our fixed host, never redirect URLs.
            query["page_token"] = token

    def _request_json(self, url: str) -> dict[str, Any]:
        if not self._api_key or not self._secret_key:
            raise DataError(f"Missing Alpaca credentials; set {self.config.alpaca_api_key_env} and {self.config.alpaca_secret_key_env}")
        for attempt in range(self.config.max_retries + 1):
            if self._last_request is not None:
                remaining = self.config.request_delay_seconds - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
            request = Request(url, headers={"APCA-API-KEY-ID": self._api_key,
                                           "APCA-API-SECRET-KEY": self._secret_key, "Accept": "application/json"})
            self._last_request = time.monotonic()
            try:
                with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                    body = response.read()
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError):
                    raise DataError("Alpaca returned invalid JSON") from None
            except HTTPError as exc:
                status = exc.code
                retryable = status == 429 or 500 <= status < 600
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if not retryable or attempt == self.config.max_retries:
                    raise DataError(f"Alpaca HTTP {status}; check API credentials, SIP access, and requested history") from None
                time.sleep(self._retry_delay(attempt, retry_after))
            except (URLError, TimeoutError, OSError):
                if attempt == self.config.max_retries:
                    raise DataError("Alpaca network request failed after retries") from None
                time.sleep(self._retry_delay(attempt, None))
            except DataError:
                raise
            except ValueError:
                raise DataError("Invalid Alpaca request or response") from None
        raise DataError("Alpaca request could not be completed")

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        delay = min(60.0, 2.0 ** min(attempt, 6))
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = delay
            if math.isfinite(seconds):
                delay = max(delay, min(60.0, seconds))
        return delay

    @staticmethod
    def _write_cache(path: Path, payload: dict[str, Any]) -> None:
        temporary: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
            os.replace(temporary, path)
        except (OSError, ValueError):
            raise DataError("Unable to write Alpaca cache") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
