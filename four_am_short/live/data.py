"""Read-only Alpaca SIP data, independent of the broker used for execution.

REST discovery includes every active/tradable US equity; current gap rankings
would miss stocks which rallied early and subsequently faded. Prior closes use
only the preceding exchange session, split-adjusted to Alpaca's current basis.
This is a live-session adapter, not a historical point-in-time split database:
``asof`` controls ticker mapping, not the date through which splits are applied.

Protocol references: docs.alpaca.markets/us/reference/stockbars,
docs.alpaca.markets/us/docs/streaming-market-data and
docs.alpaca.markets/us/docs/real-time-stock-pricing-data.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx
import websockets

from ..models import Bar, EASTERN

if TYPE_CHECKING:
    from .config import DataSettings

DATA_URL = "https://data.alpaca.markets"
SIP_URL = "wss://stream.data.alpaca.markets/v2/sip"
SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,19}\Z")
Handler = Callable[[dict[str, Any]], Awaitable[None]]


class AlpacaDataError(RuntimeError):
    """An unavailable or invalid response, never an empty successful backfill."""


@dataclass(frozen=True)
class DiscoveryResult:
    symbols: list[str]
    previous_closes: dict[str, float]
    previous_close_date: date
    market_day: bool
    warnings: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing market-data timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Market-data timestamp must have a timezone")
    return timestamp


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid price")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Invalid price")
    return result


def _bar(row: dict[str, Any]) -> Bar:
    timestamp = _timestamp(row.get("t"))
    if timestamp.second or timestamp.microsecond:
        raise ValueError("Bar timestamp must be a minute boundary")
    prices = [_positive(row.get(field)) for field in ("o", "h", "l", "c")]
    opening, high, low, close = prices
    volume = float(row.get("v", 0))
    if high < max(opening, low, close) or low > min(opening, high, close):
        raise ValueError("Inconsistent OHLC prices")
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("Invalid volume")
    return Bar(timestamp.astimezone(EASTERN), opening, high, low, close, volume)


def _symbols(symbols: list[str] | set[str]) -> list[str]:
    if any(not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) for symbol in symbols):
        raise ValueError("Invalid equity symbol")
    return sorted(set(symbols))


def _safe_error(exc: Any, settings: DataSettings) -> str:
    message = str(exc)
    for secret in (settings.api_key, settings.secret_key):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:300]


def valid_event(event: dict[str, Any], now: datetime, future_tolerance: float) -> bool:
    """Validate values, preserving raw SIP keys for the strategy engine.

    Freshness and out-of-order checks for trading belong in the engine as well;
    old bars can be legitimate late corrections and are deliberately retained.
    """
    try:
        if not SYMBOL.fullmatch(event.get("S", "")):
            return False
        timestamp = _timestamp(event.get("t"))
        if (timestamp - now).total_seconds() > future_tolerance:
            return False
        kind = event.get("T")
        if kind in {"b", "u"}:
            _bar(event)
            return timestamp + timedelta(minutes=1) <= now + timedelta(seconds=future_tolerance)
        if kind == "q":
            bid, ask = _positive(event.get("bp")), _positive(event.get("ap"))
            sizes = [float(event.get(field, 0)) for field in ("bs", "as")]
            return bid <= ask and all(math.isfinite(size) and size > 0 for size in sizes)
        if kind == "t":
            _positive(event.get("p"))
            return _positive(event.get("s")) > 0
    except (ValueError, TypeError, OverflowError):
        pass
    return False


class AlpacaData:
    """GET-only client. No account-order or trading API operation is exposed."""

    def __init__(self, settings: DataSettings, *, client: httpx.AsyncClient | None = None,
                 now: Callable[[], datetime] = _now) -> None:
        self.settings = settings
        self._now = now
        self._http = client or httpx.AsyncClient(
            headers={"APCA-API-KEY-ID": settings.api_key,
                     "APCA-API-SECRET-KEY": settings.secret_key},
            timeout=settings.rest_timeout_seconds,
        )
        self._semaphore = asyncio.Semaphore(settings.rest_concurrency)
        self._trading_url = ("https://paper-api.alpaca.markets" if settings.paper
                             else "https://api.alpaca.markets")

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        for attempt in range(3):
            try:
                async with self._semaphore:
                    response = await self._http.get(url, params=params)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                # Do not display request headers, URLs containing query data,
                # or untrusted response bodies (which can echo credentials).
                raise AlpacaDataError(f"Alpaca HTTP {exc.response.status_code} for {exc.request.url.path}") from None
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise AlpacaDataError("Alpaca request timed out or the network is unavailable") from None
                await asyncio.sleep(2 ** attempt)
            except ValueError:
                raise AlpacaDataError("Alpaca response is not valid JSON") from None
        raise AlpacaDataError("Alpaca request failed")

    async def _bars(self, symbols: list[str], params: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        async def batch(group: list[str]) -> dict[str, list[dict[str, Any]]]:
            result: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in group}
            page_token = None
            seen_tokens: set[str] = set()
            while True:
                query = {**params, "symbols": ",".join(group), "feed": "sip", "limit": 10000, "sort": "asc"}
                if page_token:
                    query["page_token"] = page_token
                payload = await self._get(DATA_URL + "/v2/stocks/bars", query)
                if not isinstance(payload, dict) or "bars" not in payload:
                    raise AlpacaDataError("Alpaca bars response is incomplete")
                rows = payload["bars"]
                if rows is None:
                    rows = {}
                if not isinstance(rows, dict):
                    raise AlpacaDataError("Alpaca bars response has invalid shape")
                for symbol, values in rows.items():
                    if symbol not in result or not isinstance(values, list):
                        raise AlpacaDataError("Alpaca bars response contains unexpected symbols or rows")
                    if any(not isinstance(value, dict) for value in values):
                        raise AlpacaDataError("Alpaca bars response contains an invalid row")
                    result[symbol].extend(values)
                page_token = payload.get("next_page_token")
                if not page_token:
                    return result
                if not isinstance(page_token, str) or page_token in seen_tokens:
                    raise AlpacaDataError("Alpaca returned an invalid/repeating pagination token")
                seen_tokens.add(page_token)

        groups = [symbols[index:index + self.settings.batch_size]
                  for index in range(0, len(symbols), self.settings.batch_size)]
        tasks = [asyncio.create_task(batch(group)) for group in groups]
        try:
            pieces = await asyncio.gather(*tasks)
        except BaseException:
            # A partial universe must not continue fetching in the background
            # while the caller is reporting a failed/retried initialization.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return {symbol: rows for piece in pieces for symbol, rows in piece.items()}

    async def discover(self, day: date) -> DiscoveryResult:
        calendar = await self._get(self._trading_url + "/v2/calendar", {
            "start": (day - timedelta(days=31)).isoformat(), "end": day.isoformat(),
        })
        try:
            if not isinstance(calendar, list):
                raise ValueError("calendar must be a list")
            sessions = sorted({date.fromisoformat(row["date"]) for row in calendar})
            previous_day = max(session for session in sessions if session < day)
        except (KeyError, TypeError, ValueError):
            raise AlpacaDataError("Cannot establish the preceding exchange session from Alpaca calendar") from None
        if day not in sessions:
            return DiscoveryResult([], {}, previous_day, False, ["The selected date is not an exchange session."])
        assets = await self._get(self._trading_url + "/v2/assets", {
            "status": "active", "asset_class": "us_equity",
        })
        if not isinstance(assets, list) or any(not isinstance(asset, dict) for asset in assets):
            raise AlpacaDataError("Alpaca assets response has invalid shape")
        symbols = sorted({asset["symbol"] for asset in assets
                          if asset.get("status") == "active" and asset.get("tradable") is True
                          and asset.get("class", asset.get("asset_class")) == "us_equity"
                          and isinstance(asset.get("symbol"), str) and SYMBOL.fullmatch(asset["symbol"])})
        if not symbols:
            raise AlpacaDataError("Alpaca returned no active tradable US equities")
        start = datetime.combine(previous_day, time.min, EASTERN)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        rows = await self._bars(symbols, {"timeframe": "1Day", "adjustment": "split",
                                         "asof": day.isoformat(), "start": start.isoformat(), "end": end.isoformat()})
        closes: dict[str, float] = {}
        for symbol, bars in rows.items():
            for row in bars:
                try:
                    if _timestamp(row.get("t")).astimezone(EASTERN).date() != previous_day:
                        continue
                    close = _positive(row.get("c"))
                except (TypeError, ValueError, OverflowError):
                    raise AlpacaDataError(f"Invalid previous-session close for {symbol}") from None
                if symbol in closes and closes[symbol] != close:
                    raise AlpacaDataError(f"Conflicting previous-session closes for {symbol}")
                closes[symbol] = close
        warnings = []
        missing = len(symbols) - len(closes)
        if missing:
            warnings.append(f"{missing} of {len(symbols)} symbols lack a valid close for {previous_day}; these cannot qualify.")
        if not closes:
            raise AlpacaDataError("No valid preceding-session SIP closes; discovery cannot be marked complete")
        return DiscoveryResult(symbols, closes, previous_day, True, warnings)

    async def backfill(self, symbols: list[str], day: date, start: str, end: str) -> dict[str, list[Bar]]:
        symbols = _symbols(symbols)
        start_at = datetime.combine(day, time.fromisoformat(start), EASTERN)
        requested_end = datetime.combine(day, time.fromisoformat(end), EASTERN)
        if start_at >= requested_end:
            raise ValueError("Backfill start must precede end")
        # Alpaca's endpoint end is inclusive. Request one microsecond before our
        # exclusive boundary and defensively filter the returned rows as well.
        complete_before = self._now().astimezone(EASTERN).replace(second=0, microsecond=0)
        end_at = min(requested_end, complete_before)
        if end_at <= start_at or not symbols:
            return {symbol: [] for symbol in symbols}
        rows = await self._bars(symbols, {"timeframe": "1Min", "adjustment": "raw", "asof": day.isoformat(),
                                         "start": start_at.isoformat(), "end": (end_at - timedelta(microseconds=1)).isoformat()})
        result: dict[str, list[Bar]] = {}
        for symbol, values in rows.items():
            by_time: dict[datetime, Bar] = {}
            for row in values:
                try:
                    bar = _bar(row)
                except (TypeError, ValueError, OverflowError):
                    raise AlpacaDataError(f"Invalid SIP backfill bar for {symbol}") from None
                if start_at <= bar.timestamp and bar.timestamp + timedelta(minutes=1) <= end_at:
                    by_time[bar.timestamp] = bar
            result[symbol] = sorted(by_time.values(), key=lambda bar: bar.timestamp)
        return result

    async def latest_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        symbols = _symbols(symbols)
        result: dict[str, dict[str, Any]] = {}
        for index in range(0, len(symbols), self.settings.batch_size):
            group = symbols[index:index + self.settings.batch_size]
            payload = await self._get(DATA_URL + "/v2/stocks/quotes/latest", {"symbols": ",".join(group), "feed": "sip"})
            if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), dict):
                raise AlpacaDataError("Alpaca latest-quotes response has invalid shape")
            for symbol, row in payload["quotes"].items():
                if symbol not in group or not isinstance(row, dict):
                    continue
                event = {**row, "T": "q", "S": symbol}
                now = self._now()
                if valid_event(event, now, self.settings.future_tolerance_seconds):
                    age = (now - _timestamp(event["t"])).total_seconds()
                    if age <= self.settings.quote_max_age_seconds:
                        result[symbol] = event
        return result


class SIPFeed:
    """Authenticated SIP wildcard bars plus dynamic candidate quotes/trades.

    Status dictionaries always include ``feed``, ``connected``, ``authenticated``,
    ``subscribed``, ``ready``, ``error``, ``last_message``, ``quotes`` and ``trades``.
    The welcome message never means ready. Any disconnect clears readiness;
    consumers must backfill the lost interval before enabling new entries.
    """

    def __init__(self, settings: DataSettings, on_event: Handler, on_status: Handler, *,
                 connect: Callable[..., Any] | None = None, now: Callable[[], datetime] = _now) -> None:
        self.settings, self.on_event, self.on_status = settings, on_event, on_status
        self._connect = connect or websockets.connect
        self._now = now
        self._desired: set[str] = set()
        self._changed = asyncio.Event()
        self._sent: set[str] = set()
        self._status: dict[str, Any] = {"feed": "sip", "connected": False, "authenticated": False,
                                      "subscribed": False, "ready": False, "error": "", "last_message": "",
                                      "quotes": [], "trades": []}

    async def _publish(self, **values: Any) -> None:
        self._status.update(values)
        await self.on_status(dict(self._status))

    async def set_symbols(self, symbols: set[str]) -> None:
        desired = set(_symbols(symbols))
        if desired != self._desired:
            self._desired = desired
            self._changed.set()

    async def _sync(self, socket: Any, *, initial: bool = False) -> None:
        desired = set(self._desired)
        removed, added = sorted(self._sent - desired), sorted(desired - self._sent)
        if removed:
            await socket.send(json.dumps({"action": "unsubscribe", "quotes": removed, "trades": removed}))
        if initial or added:
            command: dict[str, Any] = {"action": "subscribe", "quotes": sorted(desired) if initial else added,
                                       "trades": sorted(desired) if initial else added}
            if initial:
                command.update(bars=["*"], updatedBars=["*"])
            await socket.send(json.dumps(command))
        self._sent = desired

    async def _session(self, socket: Any, stop: asyncio.Event) -> None:
        await socket.send(json.dumps({"action": "auth", "key": self.settings.api_key, "secret": self.settings.secret_key}))
        receive = asyncio.create_task(socket.recv())
        changed = asyncio.create_task(self._changed.wait())
        stopped = asyncio.create_task(stop.wait())
        last_status = 0.0
        loop = asyncio.get_running_loop()
        handshake_deadline = loop.time() + 15
        try:
            while not stop.is_set():
                timeout = max(0, handshake_deadline - loop.time()) if not self._status["ready"] else None
                done, _ = await asyncio.wait((receive, changed, stopped), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise AlpacaDataError("SIP authentication/subscription confirmation timed out")
                if stopped in done:
                    return
                if changed in done:
                    self._changed.clear()
                    changed = asyncio.create_task(self._changed.wait())
                    if self._status["authenticated"]:
                        await self._sync(socket)
                if receive not in done:
                    continue
                message = receive.result()
                receive = asyncio.create_task(socket.recv())
                try:
                    events = json.loads(message)
                except (ValueError, TypeError):
                    raise AlpacaDataError("SIP returned a malformed JSON frame") from None
                if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                    raise AlpacaDataError("SIP returned an invalid frame shape")
                self._status["last_message"] = self._now().isoformat()
                for event in events:
                    kind = event.get("T")
                    if kind == "error":
                        raise AlpacaDataError(f"SIP error {event.get('code', '?')}: {_safe_error(event.get('msg', ''), self.settings)}")
                    if kind == "success" and event.get("msg") == "authenticated":
                        await self._publish(authenticated=True)
                        await self._sync(socket, initial=True)
                    elif kind == "subscription":
                        ready = (self._status["authenticated"] and "*" in event.get("bars", [])
                                 and "*" in event.get("updatedBars", []))
                        await self._publish(subscribed=bool(ready), ready=bool(ready), error="",
                                            quotes=event.get("quotes", []), trades=event.get("trades", []))
                    elif kind in {"b", "u", "q", "t"} and self._status["ready"]:
                        if valid_event(event, self._now(), self.settings.future_tolerance_seconds):
                            await self.on_event(event)
                        else:
                            await self._publish(error=f"Ignored invalid SIP {kind} event")
                if loop.time() - last_status >= 5:
                    await self._publish()
                    last_status = loop.time()
        finally:
            for task in (receive, changed, stopped):
                task.cancel()
            await asyncio.gather(receive, changed, stopped, return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                self._sent = set()
                try:
                    async with self._connect(SIP_URL, ping_interval=20, ping_timeout=20,
                                             close_timeout=5, open_timeout=15, max_queue=1024) as socket:
                        await self._publish(connected=True, authenticated=False, subscribed=False, ready=False,
                                            error="", quotes=[], trades=[])
                        await self._session(socket, stop)
                    if stop.is_set():
                        break
                    raise AlpacaDataError("SIP stream closed")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._publish(connected=False, authenticated=False, subscribed=False, ready=False,
                                        error=_safe_error(exc, self.settings), quotes=[], trades=[])
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.reconnect_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._publish(connected=False, authenticated=False, subscribed=False, ready=False,
                                quotes=[], trades=[])
