"""Provider regressions using HTTP fixtures; no credential or network required."""

import io
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from four_am_short.config import DataConfig
from four_am_short.massive import MassiveClient, _NoRedirect
from four_am_short.models import DataError


ET = ZoneInfo("America/New_York")
DAY = date(2025, 3, 10)
PREVIOUS = date(2025, 3, 7)


def bar(day=DAY, clock="04:00", *, open_price=14.0, high=15.0, low=13.0, close=14.5):
    timestamp = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=ET)
    return {"t": int(timestamp.timestamp() * 1000), "o": open_price, "h": high,
            "l": low, "c": close, "v": 1000}


def aggregates(symbol="XYZ", rows=None, **extra):
    rows = [bar()] if rows is None else rows
    return {"status": "OK", "ticker": symbol, "adjusted": False,
            "results": rows, "resultsCount": len(rows), **extra}


def calendar(*days):
    return aggregates("SPY", [bar(day, "00:00") for day in days or (PREVIOUS,)])


def daily_close(symbol="XYZ", day=PREVIOUS, close=10.0):
    return {"status": "OK", "symbol": symbol, "from": str(day),
            "close": close, "afterHours": 50.0}


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


class MassiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = DataConfig(cache_dir=Path(self.temporary.name), request_delay_seconds=0,
                                 max_retries=2)
        self.client = MassiveClient(self.config, "fixture-secret")
        self.sleep = patch("four_am_short.massive.time.sleep")
        self.sleep_mock = self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def network(self, *payloads):
        mock = patch.object(self.client._opener, "open", side_effect=[
            payload if isinstance(payload, BaseException) else response(payload)
            for payload in payloads
        ])
        result = mock.start()
        self.addCleanup(mock.stop)
        return result

    def cache_files(self):
        return list(Path(self.temporary.name).rglob("*.json"))

    def test_minute_bars_use_et_unadjusted_prices_and_header_auth(self):
        request = self.network(aggregates())
        result = self.client.minute_bars("XYZ", DAY)
        self.assertEqual(result[0].timestamp.hour, 4)
        self.assertEqual(result[0].timestamp.utcoffset().total_seconds(), -4 * 3600)
        self.assertEqual(result[0].high, 15)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer fixture-secret")
        self.assertNotIn("fixture-secret", sent.full_url)
        query = parse_qs(urlsplit(sent.full_url).query)
        self.assertEqual(query, {"adjusted": ["false"], "sort": ["asc"], "limit": ["50000"]})

    def test_previous_regular_close_uses_market_session_across_dst_weekend(self):
        request = self.network(calendar(), daily_close(), {"status": "OK", "results": []})
        result = self.client.previous_close("XYZ", DAY)
        self.assertEqual(result.trading_date, PREVIOUS)
        self.assertEqual(result.close, 10)
        self.assertEqual(result.source_close, 10)
        urls = [call.args[0].full_url for call in request.call_args_list]
        self.assertIn("/range/1/day/2025-02-24/2025-03-09", urls[0])
        self.assertIn("/v1/open-close/XYZ/2025-03-07?adjusted=false", urls[1])
        self.assertIn("/stocks/v1/splits?", urls[2])

    def test_holiday_previous_session(self):
        day = date(2025, 7, 7)
        prior = date(2025, 7, 3)
        self.network(calendar(date(2025, 7, 2), prior), daily_close(day=prior),
                     {"status": "OK", "results": []})
        self.assertEqual(self.client.previous_close("XYZ", day).trading_date, prior)

    def test_calendar_is_shared_across_symbols(self):
        request = self.network(calendar(), daily_close(), {"status": "OK", "results": []},
                               daily_close("ABC"), {"status": "OK", "results": []})
        self.client.previous_close("XYZ", DAY)
        self.client.previous_close("ABC", DAY)
        self.assertEqual(request.call_count, 5)

    def test_split_normalizes_prior_close_without_adjusting_historical_bar_prices(self):
        split = {"ticker": "XYZ", "execution_date": str(DAY), "split_from": 10, "split_to": 1}
        self.network(calendar(), daily_close(close=1), {"status": "OK", "results": [split]})
        result = self.client.previous_close("XYZ", DAY)
        self.assertEqual((result.source_close, result.close, result.split_factor), (1, 10, 10))

    def test_forward_split_pages_combine_all_events(self):
        first = {"ticker": "XYZ", "execution_date": str(DAY), "split_from": 1, "split_to": 2, "id": "a"}
        second = {**first, "split_to": 3, "id": "b"}
        request = self.network(calendar(), daily_close(close=60),
            {"status": "OK", "results": [first], "next_url": "https://api.massive.com/stocks/v1/splits?cursor=two"},
            {"status": "OK", "results": [second]})
        result = self.client.previous_close("XYZ", DAY)
        self.assertAlmostEqual(result.close, 10)
        self.assertEqual(request.call_count, 4)

    def test_missing_previous_session_close_never_falls_back_to_stale_day(self):
        request = self.network(calendar(), {"status": "NOT_FOUND"})
        with self.assertRaisesRegex(DataError, "status OK"):
            self.client.previous_close("XYZ", DAY)
        self.assertEqual(request.call_count, 2)

    def test_previous_close_rejects_wrong_symbol_or_date(self):
        self.network(calendar(), daily_close(day=date(2025, 3, 6)))
        with self.assertRaisesRegex(DataError, "requested ticker/date"):
            self.client.previous_close("XYZ", DAY)

    def test_split_rejects_wrong_day(self):
        split = {"ticker": "XYZ", "execution_date": "2025-03-11", "split_from": 10, "split_to": 1}
        self.network(calendar(), daily_close(), {"status": "OK", "results": [split]})
        with self.assertRaisesRegex(DataError, "requested ticker/date"):
            self.client.previous_close("XYZ", DAY)

    def test_complete_cache_works_offline_without_key(self):
        self.network(calendar(), daily_close(), {"status": "OK", "results": []}, aggregates())
        expected_close = self.client.previous_close("XYZ", DAY)
        expected_bars = self.client.minute_bars("XYZ", DAY)
        offline = MassiveClient(self.config, None, offline=True)
        with patch.object(offline._opener, "open", side_effect=AssertionError("network called")):
            self.assertEqual(offline.previous_close("XYZ", DAY), expected_close)
            self.assertEqual(offline.minute_bars("XYZ", DAY), expected_bars)
        self.assertTrue(self.cache_files())
        for filename in self.cache_files():
            self.assertNotIn("fixture-secret", filename.read_text())
        self.assertFalse(list(Path(self.temporary.name).rglob("*.tmp")))

    def test_offline_cache_miss_is_explicit(self):
        with self.assertRaisesRegex(DataError, "Offline Massive cache miss"):
            MassiveClient(self.config, None, offline=True).minute_bars("XYZ", DAY)

    def test_refresh_bypasses_cache(self):
        self.network(aggregates())
        self.client.minute_bars("XYZ", DAY)
        fresh = MassiveClient(self.config, "fixture-secret", refresh_cache=True)
        with patch.object(fresh._opener, "open", return_value=response(aggregates(rows=[]))):
            self.assertEqual(fresh.minute_bars("XYZ", DAY), [])
        self.assertEqual(MassiveClient(self.config, None, offline=True).minute_bars("XYZ", DAY), [])

    def test_cache_identity_and_completeness_are_checked(self):
        self.network(aggregates())
        self.client.minute_bars("XYZ", DAY)
        filename = self.cache_files()[0]
        original = json.loads(filename.read_text())
        for attribute, replacement in (("request", {}), ("complete", False), ("version", 999)):
            with self.subTest(attribute=attribute):
                tampered = {**original, attribute: replacement}
                filename.write_text(json.dumps(tampered))
                with self.assertRaisesRegex(DataError, "cache"):
                    MassiveClient(self.config, None, offline=True).minute_bars("XYZ", DAY)

    def test_corrupt_cache_is_reported_with_refresh_remedy(self):
        self.network(aggregates())
        self.client.minute_bars("XYZ", DAY)
        self.cache_files()[0].write_text("{not json")
        with self.assertRaisesRegex(DataError, "refresh-cache"):
            self.client.minute_bars("XYZ", DAY)

    def test_today_uses_fresh_data_without_reading_or_writing_cache(self):
        today = datetime.now(ET).date()
        request = self.network(aggregates(rows=[bar(today)]), aggregates(rows=[]))
        self.assertEqual(len(self.client.minute_bars("XYZ", today)), 1)
        self.assertEqual(self.client.minute_bars("XYZ", today), [])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.cache_files(), [])
        with self.assertRaisesRegex(DataError, "completed historical"):
            MassiveClient(self.config, None, offline=True).minute_bars("XYZ", today)

    def test_pagination_is_complete_and_strips_query_credentials(self):
        request = self.network(
            aggregates(next_url="https://api.massive.com/v2/aggs/ticker/XYZ/range/1/minute/2025-03-10/2025-03-10?cursor=two&apiKey=secret-from-server"),
            aggregates(rows=[bar(clock="04:02")]),
        )
        self.assertEqual(len(self.client.minute_bars("XYZ", DAY)), 2)
        self.assertNotIn("apiKey", request.call_args.args[0].full_url)
        self.assertNotIn("secret-from-server", self.cache_files()[0].read_text())
        self.assertNotIn("next_url", self.cache_files()[0].read_text())

    def test_unsafe_pagination_never_receives_authorization(self):
        for url in ("https://evil.example/page", "http://api.massive.com/page",
                    "https://api.massive.com.evil.example/page", "https://api.massive.com@evil.example/page",
                    "https://api.massive.com:8443/page", "/relative/page"):
            with self.subTest(url=url):
                with patch.object(self.client._opener, "open", return_value=response(aggregates(next_url=url))) as request:
                    with self.assertRaisesRegex(DataError, "pagination URL"):
                        self.client.minute_bars("XYZ", DAY)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(self.cache_files(), [])

    def test_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))
        request = self.network(HTTPError("https://api.massive.com", 302, "Found", {}, None))
        with self.assertRaisesRegex(DataError, "HTTP 302"):
            self.client.minute_bars("XYZ", DAY)
        self.assertEqual(request.call_count, 1)

    def test_duplicate_bars_across_pages_are_errors_not_silently_deduplicated(self):
        self.network(aggregates(next_url="https://api.massive.com/page?cursor=two"), aggregates())
        with self.assertRaisesRegex(DataError, "duplicate"):
            self.client.minute_bars("XYZ", DAY)
        self.assertEqual(self.cache_files(), [])

    def test_missing_minutes_remain_missing(self):
        self.network(aggregates(rows=[bar(clock="04:00"), bar(clock="04:03")]))
        bars = self.client.minute_bars("XYZ", DAY)
        self.assertEqual([item.timestamp.minute for item in bars], [0, 3])

    def test_empty_aggregate_response_without_results_is_valid(self):
        self.network({"status": "OK", "ticker": "XYZ", "adjusted": False, "resultsCount": 0})
        self.assertEqual(self.client.minute_bars("XYZ", DAY), [])

    def test_invalid_aggregate_payloads_are_not_cached(self):
        invalid = [
            {"status": "ERROR", "message": "fixture-secret"},
            aggregates(adjusted=True), aggregates(ticker="WRONG"),
            aggregates(resultsCount=2), aggregates(rows=[{**bar(), "c": 100}]),
            aggregates(rows=[{**bar(), "o": 0}]), aggregates(rows=[{**bar(), "o": "14"}]),
            aggregates(rows=[{**bar(), "h": float("nan")}]),
            aggregates(rows=[{**bar(), "t": bar()["t"] + 1}]),
            aggregates(rows=[bar(day=PREVIOUS)]), aggregates(results="bad"), [],
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with patch.object(self.client._opener, "open", return_value=response(payload)):
                    with self.assertRaises(DataError) as error:
                        self.client.minute_bars("XYZ", DAY)
                    self.assertNotIn("fixture-secret", str(error.exception))
                    self.assertEqual(self.cache_files(), [])

    def test_invalid_json_is_reported_without_raw_body(self):
        with patch.object(self.client._opener, "open", return_value=io.BytesIO(b"fixture-secret")):
            with self.assertRaisesRegex(DataError, "invalid JSON") as error:
                self.client.minute_bars("XYZ", DAY)
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_rate_limit_and_server_error_retries_then_succeeds(self):
        request = self.network(
            HTTPError("https://api.massive.com", 429, "fixture-secret", {"Retry-After": "3"}, None),
            HTTPError("https://api.massive.com", 503, "fixture-secret", {}, None),
            aggregates(),
        )
        self.assertEqual(len(self.client.minute_bars("XYZ", DAY)), 1)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep_mock.call_args_list], [3, 2])

    def test_auth_error_does_not_retry_or_leak_key(self):
        request = self.network(HTTPError("https://api.massive.com", 403, "fixture-secret", {}, None))
        with self.assertRaisesRegex(DataError, "HTTP 403") as error:
            self.client.minute_bars("XYZ", DAY)
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.cache_files(), [])

    def test_network_retry_exhaustion_is_sanitized(self):
        request = self.network(*(URLError("fixture-secret") for _ in range(3)))
        with self.assertRaisesRegex(DataError, "network request failed") as error:
            self.client.minute_bars("XYZ", DAY)
        self.assertNotIn("fixture-secret", str(error.exception))
        self.assertEqual(request.call_count, 3)

    def test_missing_key_is_explicit_and_does_not_send_network_request(self):
        client = MassiveClient(self.config, None)
        with patch.object(client._opener, "open") as request:
            with self.assertRaisesRegex(DataError, "MASSIVE_API_KEY"):
                client.minute_bars("XYZ", DAY)
            request.assert_not_called()

    def test_offline_and_refresh_cannot_be_combined(self):
        with self.assertRaisesRegex(DataError, "cannot refresh"):
            MassiveClient(self.config, None, offline=True, refresh_cache=True)

    def test_invalid_api_key_format_never_exposes_key(self):
        for key in ("fixture-secret\r\nX-Bad: header", "fixture-secret\x00", "fixture-secret\u0100"):
            with self.subTest(kind=repr(key[-1:])):
                with self.assertRaisesRegex(DataError, "Invalid Massive API key format") as error:
                    MassiveClient(self.config, key)
                self.assertNotIn("fixture-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
