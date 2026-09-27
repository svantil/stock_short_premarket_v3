"""Massive historical/intraday prices, with HTTPS and validated local caches.

All prices are as traded (``adjusted=false``). Only the prior regular close is
normalized for share changes effective on the requested trading date.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import DataConfig
from .models import Bar, DataError, PreviousClose


EASTERN = ZoneInfo("America/New_York")
API_BASE = "https://api.massive.com"
CACHE_VERSION = 1
T = TypeVar("T")


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward the authorization header to an HTTP redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value: Any, field: str, *, positive: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError(f"Massive returned a nonnumeric {field}")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise DataError(f"Massive returned an invalid {field}") from None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        raise DataError(f"Massive returned an invalid {field}")
    return number


def _symbol(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", value):
        raise DataError("Invalid stock ticker")
    return value


def _safe_url(value: str) -> str:
    """Validate pagination destinations and remove any query credentials."""
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise DataError("Massive returned an invalid pagination URL")
    try:
        parsed = urlsplit(value)
        safe = (
            parsed.scheme == "https"
            and parsed.hostname == "api.massive.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.startswith("/")
        )
    except ValueError:
        safe = False
    if not safe:
        raise DataError("Refusing unsafe Massive pagination URL")
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if key.lower() not in {"apikey", "api_key", "authorization", "token", "access_token"}]
    return urlunsplit(("https", "api.massive.com", parsed.path, urlencode(query), ""))


class MassiveClient:
    def __init__(
        self,
        config: DataConfig,
        api_key: str | None,
        *,
        offline: bool = False,
        refresh_cache: bool = False,
    ) -> None:
        if offline and refresh_cache:
            raise DataError("Offline mode cannot refresh the Massive cache")
        if api_key and any(ord(char) <= 32 or ord(char) >= 127 for char in api_key):
            raise DataError("Invalid Massive API key format")
        self.config = config
        self._api_key = api_key.strip() if api_key else None
        self.offline = offline
        self.refresh_cache = refresh_cache
        self._opener = build_opener(_NoRedirect())
        self._last_request: float | None = None
        self._sessions: dict[date, date] = {}

    def minute_bars(self, symbol: str, day: date) -> list[Bar]:
        symbol = _symbol(symbol)
        path = f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/1/minute/{day}/{day}"
        return self._cached(
            path,
            {"adjusted": "false", "sort": "asc", "limit": "50000"},
            lambda pages: self._decode_bars(pages, symbol, day, day, minute=True),
            cacheable=day < datetime.now(EASTERN).date(),
        )

    def previous_close(self, symbol: str, day: date) -> PreviousClose:
        symbol = _symbol(symbol)
        previous_day = self._previous_session(day)
        path = f"/v1/open-close/{quote(symbol, safe='')}/{previous_day}"

        def decode_close(pages: list[dict[str, Any]]) -> float:
            if len(pages) != 1:
                raise DataError("Unexpected pagination for Massive daily close")
            result = pages[0]
            if result.get("symbol") != symbol or result.get("from") != str(previous_day):
                raise DataError("Massive daily close does not match the requested ticker/date")
            if "adjusted" in result and result["adjusted"] is not False:
                raise DataError("Massive daily close was unexpectedly split-adjusted")
            return _number(result.get("close"), "regular-session close")

        source_close = self._cached(path, {"adjusted": "false"}, decode_close)
        split_factor = self._split_factor(symbol, day)
        close = _number(source_close * split_factor, "split-normalized prior close")
        return PreviousClose(
            trading_date=previous_day,
            close=close,
            source_close=source_close,
            split_factor=split_factor,
        )

    def _previous_session(self, day: date) -> date:
        if day in self._sessions:
            return self._sessions[day]
        first = day - timedelta(days=self.config.previous_close_lookback_days)
        last = day - timedelta(days=1)
        calendar_symbol = _symbol(self.config.calendar_symbol)
        path = f"/v2/aggs/ticker/{quote(calendar_symbol, safe='')}/range/1/day/{first}/{last}"
        bars = self._cached(
            path,
            {"adjusted": "false", "sort": "asc", "limit": "50000"},
            lambda pages: self._decode_bars(pages, calendar_symbol, first, last, minute=False),
        )
        if not bars:
            raise DataError(f"No previous market session found for {day} using {calendar_symbol}")
        previous_day = max(bar.timestamp.date() for bar in bars)
        self._sessions[day] = previous_day
        return previous_day

    def _split_factor(self, symbol: str, day: date) -> float:
        def decode(pages: list[dict[str, Any]]) -> float:
            factor = 1.0
            seen: set[str] = set()
            for page in pages:
                if "results" not in page:
                    raise DataError("Massive split response is missing results")
                for row in self._results(page):
                    if row.get("ticker") != symbol or row.get("execution_date") != str(day):
                        raise DataError("Massive split does not match the requested ticker/date")
                    identity = str(row.get("id") or json.dumps(row, sort_keys=True))
                    if identity in seen:
                        raise DataError("Massive returned duplicate split records")
                    seen.add(identity)
                    factor *= _number(row.get("split_from"), "split_from") / _number(
                        row.get("split_to"), "split_to"
                    )
            return _number(factor, "split factor")

        return self._cached(
            "/stocks/v1/splits",
            {"ticker": symbol, "execution_date": str(day), "limit": "5000", "sort": "execution_date.asc"},
            decode,
        )

    @staticmethod
    def _results(page: dict[str, Any]) -> list[dict[str, Any]]:
        # Aggregate endpoints omit results when zero eligible trades exist.
        results = page.get("results", [])
        if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
            raise DataError("Massive returned invalid results")
        count = page.get("resultsCount")
        if count is not None and (type(count) is not int or count != len(results)):
            raise DataError("Massive result count does not match the response")
        return results

    def _decode_bars(
        self,
        pages: list[dict[str, Any]],
        symbol: str,
        first: date,
        last: date,
        *,
        minute: bool,
    ) -> list[Bar]:
        bars: list[Bar] = []
        seen: set[datetime | date] = set()
        for page in pages:
            if page.get("ticker") != symbol:
                raise DataError("Massive aggregate ticker does not match the request")
            if page.get("adjusted") is not False:
                raise DataError("Massive aggregates must explicitly be unadjusted")
            for row in self._results(page):
                stamp = row.get("t")
                if type(stamp) is not int or (minute and stamp % 60000):
                    raise DataError("Massive returned an invalid bar timestamp")
                try:
                    timestamp = datetime.fromtimestamp(stamp / 1000, tz=EASTERN)
                except (OSError, OverflowError, ValueError):
                    raise DataError("Massive returned an invalid bar timestamp") from None
                if not first <= timestamp.date() <= last:
                    raise DataError("Massive returned a bar outside the requested Eastern dates")
                identity = timestamp if minute else timestamp.date()
                if identity in seen:
                    raise DataError("Massive returned duplicate aggregate timestamps")
                seen.add(identity)
                open_price = _number(row.get("o"), "open")
                high = _number(row.get("h"), "high")
                low = _number(row.get("l"), "low")
                close = _number(row.get("c"), "close")
                if not low <= min(open_price, close) <= max(open_price, close) <= high:
                    raise DataError("Massive returned inconsistent OHLC prices")
                bars.append(Bar(
                    timestamp=timestamp,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=_number(row.get("v", 0), "volume", positive=False),
                ))
        bars.sort(key=lambda bar: bar.timestamp)
        return bars

    def _cached(
        self,
        path: str,
        params: dict[str, str],
        decode: Callable[[list[dict[str, Any]]], T],
        *,
        cacheable: bool = True,
    ) -> T:
        if not cacheable:
            if self.offline:
                raise DataError("Offline Massive data requires a completed historical Eastern date")
            return decode(self._request_pages(path, params))
        identity = {"base_url": API_BASE, "path": path, "params": params}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache_file = Path(self.config.cache_dir) / "massive-v1" / f"{digest}.json"
        if not self.refresh_cache and cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get("version") != CACHE_VERSION
                    or payload.get("request") != identity
                    or payload.get("complete") is not True
                ):
                    raise DataError("Massive cache identity/completeness check failed")
                pages = payload.get("pages")
                self._validate_pages(pages)
                return decode(pages)
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                if isinstance(exc, DataError):
                    raise DataError(f"Invalid Massive cache; use --refresh-cache: {exc}") from None
                raise DataError("Unreadable Massive cache; use --refresh-cache") from None
        if self.offline:
            raise DataError(f"Offline Massive cache miss: {path}")
        pages = self._request_pages(path, params)
        result = decode(pages)  # Invalid or incomplete responses are never cached.
        payload = {"version": CACHE_VERSION, "request": identity, "complete": True, "pages": pages}
        self._write_cache(cache_file, payload)
        return result

    @staticmethod
    def _validate_pages(pages: Any) -> None:
        if not isinstance(pages, list) or not pages:
            raise DataError("Massive response has no complete pages")
        for page in pages:
            if not isinstance(page, dict) or page.get("status") not in ("OK", "DELAYED"):
                raise DataError("Massive API did not return status OK or DELAYED")
            if "next_url" in page:
                raise DataError("Massive cache contains unfinished pagination")

    def _request_pages(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        url = _safe_url(f"{API_BASE}{path}?{urlencode(sorted(params.items()))}")
        pages: list[dict[str, Any]] = []
        seen: set[str] = set()
        while url:
            if url in seen or len(seen) >= 10000:
                raise DataError("Massive pagination did not terminate")
            seen.add(url)
            page = self._request_json(url)
            # Intraday aggregates on delayed plans contain valid bars with this status.
            if not isinstance(page, dict) or page.get("status") not in ("OK", "DELAYED"):
                raise DataError("Massive API did not return status OK or DELAYED")
            next_url = page.pop("next_url", None)
            pages.append(page)
            url = _safe_url(next_url) if next_url else ""
        self._validate_pages(pages)
        return pages

    def _request_json(self, url: str) -> dict[str, Any]:
        if not self._api_key:
            raise DataError(f"Missing Massive API key; set {self.config.api_key_env}")
        for attempt in range(self.config.max_retries + 1):
            if self._last_request is not None:
                remaining = self.config.request_delay_seconds - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
            request = Request(url, headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"})
            self._last_request = time.monotonic()
            try:
                with self._opener.open(request, timeout=self.config.timeout_seconds) as response:
                    body = response.read()
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError):
                    raise DataError("Massive returned invalid JSON") from None
            except HTTPError as exc:
                status = exc.code
                retryable = status == 429 or 500 <= status < 600
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if not retryable or attempt == self.config.max_retries:
                    raise DataError(f"Massive HTTP {status}; check API access, plan, and requested history") from None
                time.sleep(self._retry_delay(attempt, retry_after))
            except (URLError, TimeoutError, OSError):
                if attempt == self.config.max_retries:
                    raise DataError("Massive network request failed after retries") from None
                time.sleep(self._retry_delay(attempt, None))
            except DataError:
                raise
            except ValueError:
                raise DataError("Invalid Massive request or response") from None
        raise DataError("Massive request could not be completed")

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str | None) -> float:
        delay = min(60.0, 2.0 ** min(attempt, 6))
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    then = parsedate_to_datetime(retry_after)
                    seconds = (then - datetime.now(timezone.utc)).total_seconds()
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
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"), allow_nan=False)
                handle.write("\n")
            os.replace(temporary, path)
        except (OSError, ValueError):
            raise DataError("Unable to write Massive cache") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
