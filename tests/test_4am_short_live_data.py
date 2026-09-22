"""Offline REST/stream protocol tests; no credentials or market connections."""

import asyncio
import json
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx

from four_am_short.live.data import AlpacaData, AlpacaDataError, SIPFeed, SIP_URL, valid_event


DAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 8, 16, 12, tzinfo=timezone.utc)


def settings(**changes):
    return SimpleNamespace(**{
        **dict(api_key="example-key", secret_key="example-secret", paper=False,
               rest_timeout_seconds=20, batch_size=200, rest_concurrency=3,
               reconnect_seconds=.001, quote_max_age_seconds=5, future_tolerance_seconds=1),
        **changes,
    })


def bar(stamp="2026-09-21T08:04:00Z", **changes):
    return {**dict(t=stamp, o=13, h=15, l=12, c=14, v=100), **changes}


def quote(stamp="2026-09-21T08:16:11.123456789Z", **changes):
    return {**{"t": stamp, "bp": 14, "ap": 14.1, "bs": 4, "as": 5}, **changes}


def asset(symbol, **changes):
    return {**dict(symbol=symbol, status="active", tradable=True, shortable=False,
                   **{"class": "us_equity"}), **changes}


class DataTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler, **changes):
        client = AlpacaData(settings(**changes), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), now=lambda: NOW)
        self.addAsyncCleanup(client.close)
        return client

    async def test_discovery_exact_prior_session_and_split_adjustment(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            if request.url.path == "/v2/calendar":
                return httpx.Response(200, json=[{"date": "2026-09-17"}, {"date": "2026-09-18"}, {"date": "2026-09-21"}])
            if request.url.path == "/v2/assets":
                return httpx.Response(200, json=[asset("FADE"), asset("SPLIT"), asset("STALE"), asset("NO", tradable=False)])
            self.assertEqual(request.url.params["adjustment"], "split")
            self.assertEqual(request.url.params["asof"], "2026-09-21")
            self.assertEqual(request.url.params["feed"], "sip")
            self.assertTrue(request.url.params["start"].startswith("2026-09-18T00:00"))
            return httpx.Response(200, json={"bars": {
                "FADE": [bar("2026-09-18T04:00:00Z", c=10)],
                "SPLIT": [bar("2026-09-18T04:00:00Z", c=20)],
                "STALE": [bar("2026-09-17T04:00:00Z", c=7)],
            }, "next_page_token": None})

        result = await self.client(handler).discover(DAY)
        self.assertEqual(result.symbols, ["FADE", "SPLIT", "STALE"])
        self.assertEqual(result.previous_closes, {"FADE": 10, "SPLIT": 20})
        self.assertEqual(result.previous_close_date, date(2026, 9, 18))
        self.assertTrue(result.market_day)
        self.assertIn("1 of 3", result.warnings[0])
        self.assertEqual(requests[0].url.host, "api.alpaca.markets")

    async def test_non_market_day_does_not_load_assets_or_bars(self):
        def handler(request):
            self.assertEqual(request.url.path, "/v2/calendar")
            self.assertEqual(request.url.host, "paper-api.alpaca.markets")
            return httpx.Response(200, json=[{"date": "2026-09-18"}])
        result = await self.client(handler, paper=True).discover(date(2026, 9, 20))
        self.assertFalse(result.market_day)
        self.assertEqual(result.symbols, [])

    async def test_invalid_calendar_is_an_error(self):
        with self.assertRaisesRegex(AlpacaDataError, "preceding exchange session"):
            await self.client(lambda _: httpx.Response(200, json=[])).discover(DAY)

    async def test_no_priors_is_not_successful_discovery(self):
        def handler(request):
            if request.url.path == "/v2/calendar":
                return httpx.Response(200, json=[{"date": "2026-09-18"}, {"date": "2026-09-21"}])
            if request.url.path == "/v2/assets":
                return httpx.Response(200, json=[asset("NEW")])
            return httpx.Response(200, json={"bars": {}})
        with self.assertRaisesRegex(AlpacaDataError, "No valid preceding-session"):
            await self.client(handler).discover(DAY)

    async def test_backfill_all_symbols_and_all_pages(self):
        calls = []

        def handler(request):
            calls.append(request.url.params)
            self.assertEqual(request.url.params["symbols"], "FADE,OTHER")
            self.assertEqual(request.url.params["adjustment"], "raw")
            self.assertEqual(request.url.params["feed"], "sip")
            if "page_token" not in request.url.params:
                return httpx.Response(200, json={"bars": {"FADE": [bar()]}, "next_page_token": "next"})
            return httpx.Response(200, json={"bars": {"OTHER": [bar("2026-09-21T08:14:00Z")]}, "next_page_token": None})

        result = await self.client(handler).backfill(["OTHER", "FADE"], DAY, "04:00", "04:15")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result["FADE"]), 1)
        self.assertEqual(len(result["OTHER"]), 1)
        self.assertEqual(result["FADE"][0].timestamp.hour, 4)
        self.assertTrue(calls[0]["end"].startswith("2026-09-21T04:14:59.999999"))

    async def test_backfill_filters_exclusive_end_and_incomplete_current_bar(self):
        def handler(request):
            self.assertTrue(request.url.params["end"].startswith("2026-09-21T04:15:59.999999"))
            return httpx.Response(200, json={"bars": {"A": [bar("2026-09-21T08:15:00Z"), bar("2026-09-21T08:16:00Z")]}})
        result = await self.client(handler).backfill(["A", "MISSING"], DAY, "04:00", "06:00")
        self.assertEqual(len(result["A"]), 1)
        self.assertEqual(result["MISSING"], [])

    async def test_backfill_does_not_request_future_window(self):
        def handler(_):
            self.fail("No HTTP request should be made before the window")
        self.assertEqual(await self.client(handler).backfill(["A"], DAY, "05:00", "06:00"), {"A": []})

    async def test_failed_or_malformed_backfill_cannot_look_empty(self):
        for response in [httpx.Response(403, json={"message": "not entitled"}),
                         httpx.Response(200, json={"message": "not bars"}),
                         httpx.Response(200, json={"bars": {"A": [bar(h="NaN")]}})]:
            with self.subTest(status=response.status_code), self.assertRaises(AlpacaDataError):
                await self.client(lambda _, response=response: response).backfill(["A"], DAY, "04:00", "04:15")

    async def test_pagination_token_loop_is_an_error(self):
        with self.assertRaisesRegex(AlpacaDataError, "pagination"):
            await self.client(lambda _: httpx.Response(200, json={"bars": {}, "next_page_token": "same"})).backfill(["A"], DAY, "04:00", "04:15")

    async def test_latest_quotes_reject_stale_future_crossed_and_invalid(self):
        def handler(request):
            self.assertEqual(request.url.params["feed"], "sip")
            return httpx.Response(200, json={"quotes": {
                "GOOD": quote(), "STALE": quote("2026-09-21T08:15:00Z"),
                "FUTURE": quote("2026-09-21T08:16:15Z"), "CROSS": quote(bp=15),
                "ZERO": quote(ap=0), "NAIVE": quote("2026-09-21T08:16:11"),
            }})
        quotes = await self.client(handler).latest_quotes(["GOOD", "STALE", "FUTURE", "CROSS", "ZERO", "NAIVE"])
        self.assertEqual(list(quotes), ["GOOD"])
        self.assertEqual(quotes["GOOD"]["T"], "q")
        self.assertEqual(quotes["GOOD"]["S"], "GOOD")

    def test_validation_preserves_late_corrections_but_not_incomplete_bars(self):
        self.assertTrue(valid_event({"T": "u", "S": "A", **bar()}, NOW, 1))
        self.assertFalse(valid_event({"T": "b", "S": "A", **bar("2026-09-21T08:16:00Z")}, NOW, 1))
        self.assertFalse(valid_event({"T": "t", "S": "A", "t": NOW.isoformat(), "p": -1, "s": 1}, NOW, 1))


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.on_send = None

    async def send(self, message):
        row = json.loads(message)
        self.sent.append(row)
        if self.on_send:
            await self.on_send(row)

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    def push(self, *events):
        self.incoming.put_nowait(list(events))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


def subscription(symbols=()):
    return {"T": "subscription", "bars": ["*"], "updatedBars": ["*"], "trades": list(symbols), "quotes": list(symbols)}


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_welcome_authentication_and_subscription_readiness(self):
        socket = FakeSocket()
        statuses, events = [], []
        stop = asyncio.Event()

        async def status(row):
            statuses.append(row)

        async def event(row):
            events.append(row)
            stop.set()

        async def send(row):
            if row["action"] == "auth":
                socket.push({"T": "success", "msg": "connected"})
                socket.push({"T": "q", "S": "A", **quote()})
                socket.push({"T": "success", "msg": "authenticated"})
            else:
                self.assertTrue(any(s["authenticated"] for s in statuses))
                self.assertFalse(any(s["ready"] for s in statuses))
                self.assertEqual(row["bars"], ["*"])
                self.assertEqual(row["updatedBars"], ["*"])
                socket.push(subscription(["A"]))
                socket.push({"T": "q", "S": "A", **quote()})

        def connect(url, **kwargs):
            self.assertEqual(url, SIP_URL)
            return socket

        socket.on_send = send
        feed = SIPFeed(settings(), event, status, connect=connect, now=lambda: NOW)
        await feed.set_symbols({"A"})
        await asyncio.wait_for(feed.run(stop), 2)
        self.assertEqual(len(events), 1)
        self.assertTrue(any(s["ready"] for s in statuses))
        self.assertFalse(statuses[-1]["connected"])
        self.assertFalse(statuses[-1]["ready"])

    async def test_reconnect_preserves_desired_symbols_and_clears_readiness(self):
        sockets = [FakeSocket(), FakeSocket()]
        statuses = []
        stop = asyncio.Event()
        connections = []

        async def status(row):
            statuses.append(row)

        async def event(_):
            stop.set()

        for index, socket in enumerate(sockets):
            async def send(row, socket=socket, index=index):
                if row["action"] == "auth":
                    socket.push({"T": "success", "msg": "authenticated"})
                else:
                    self.assertEqual(row["quotes"], ["A", "B"])
                    socket.push(subscription(["A", "B"]))
                    if index == 0:
                        socket.incoming.put_nowait(ConnectionError("lost feed"))
                    else:
                        socket.push({"T": "b", "S": "A", **bar()})
            socket.on_send = send

        def connect(url, **kwargs):
            connections.append(url)
            return sockets[len(connections) - 1]

        feed = SIPFeed(settings(), event, status, connect=connect, now=lambda: NOW)
        await feed.set_symbols({"A", "B"})
        await asyncio.wait_for(feed.run(stop), 2)
        self.assertEqual(connections, [SIP_URL, SIP_URL])
        self.assertTrue(any(not s["ready"] and s["error"] == "lost feed" for s in statuses))

    async def test_dynamic_unsubscribe_and_subscribe(self):
        socket = FakeSocket()
        stop = asyncio.Event()
        changed = False

        async def status(row):
            nonlocal changed
            if row["ready"] and not changed:
                changed = True
                await feed.set_symbols({"B"})

        async def event(_):
            pass

        async def send(row):
            if row["action"] == "auth":
                socket.push({"T": "success", "msg": "authenticated"})
            elif "bars" in row:
                socket.push(subscription(["A"]))
            elif row["action"] == "subscribe":
                stop.set()

        socket.on_send = send
        feed = SIPFeed(settings(), event, status, connect=lambda *a, **k: socket, now=lambda: NOW)
        await feed.set_symbols({"A"})
        await asyncio.wait_for(feed.run(stop), 2)
        self.assertIn({"action": "unsubscribe", "quotes": ["A"], "trades": ["A"]}, socket.sent)
        self.assertIn({"action": "subscribe", "quotes": ["B"], "trades": ["B"]}, socket.sent)

    async def test_entitlement_error_never_falls_back_or_marks_ready(self):
        socket = FakeSocket()
        stop = asyncio.Event()
        statuses = []

        async def status(row):
            statuses.append(row)
            if row["error"]:
                stop.set()

        async def event(_):
            self.fail("No data should pass before authentication")

        socket.push({"T": "error", "code": 409, "msg": "insufficient subscription example-secret"})
        feed = SIPFeed(settings(), event, status, connect=lambda *a, **k: socket, now=lambda: NOW)
        await asyncio.wait_for(feed.run(stop), 2)
        self.assertFalse(any(s["ready"] for s in statuses))
        self.assertIn("SIP error 409", statuses[-1]["error"])
        self.assertNotIn("example-secret", json.dumps(statuses))

    async def test_shutdown_does_not_wait_for_another_message(self):
        socket = FakeSocket()
        stop = asyncio.Event()

        async def status(row):
            if row["ready"]:
                stop.set()

        async def event(_):
            pass

        socket.push({"T": "success", "msg": "authenticated"})
        socket.push(subscription())
        feed = SIPFeed(settings(), event, status, connect=lambda *a, **k: socket, now=lambda: NOW)
        await asyncio.wait_for(feed.run(stop), 2)


if __name__ == "__main__":
    unittest.main()
