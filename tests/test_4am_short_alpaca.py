"""Historical SIP adapter regressions using HTTP fixtures, never credentials."""

import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from four_am_short.alpaca import AlpacaClient, _NoRedirect
from four_am_short.config import DataConfig
from four_am_short.models import DataError


ET = ZoneInfo("America/New_York")
DAY = date(2025, 3, 10)
PREVIOUS = date(2025, 3, 7)


def bar(day=DAY, clock="04:00", *, price=10, scale=1):
    timestamp = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=ET)
    return {"t": timestamp.isoformat(), "o": price * scale, "h": (price + 1) * scale,
            "l": (price - 1) * scale, "c": price * scale, "v": 1000}


def bars(symbol="XYZ", rows=None, **extra):
    return {"bars": {symbol: [bar()] if rows is None else rows}, "next_page_token": None, **extra}


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


class AlpacaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config = DataConfig(cache_dir=self.directory, request_delay_seconds=0, max_retries=2)
        self.client = AlpacaClient(self.config, "fixture-key", "fixture-secret")
        sleep = patch("four_am_short.alpaca.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def network(self, *payloads):
        handle = patch.object(self.client._opener, "open", side_effect=[
            payload if isinstance(payload, BaseException) else response(payload) for payload in payloads])
        result = handle.start()
        self.addCleanup(handle.stop)
        return result

    def calibration(self, *, prior=PREVIOUS, day=DAY, split=1, future=1):
        return [bars("SPY", [bar(prior, "00:00")]),
                bars(rows=[bar(prior, "00:00", price=10)]),
                bars(rows=[bar(prior, "00:00", price=10, scale=split * future)]),
                bars(rows=[bar(day, price=15)]),
                bars(rows=[bar(day, price=15, scale=future)])]

    def files(self):
        return list(self.directory.rglob("*.json"))

    def test_raw_sip_parameters_include_historical_mapping_and_full_eastern_day(self):
        request = self.network(bars())
        result = self.client.minute_bars("xyz", DAY)
        self.assertEqual(result[0].timestamp.hour, 4)
        self.assertEqual(result[0].high, 11)
        sent = request.call_args.args[0]
        self.assertEqual(sent.get_header("Apca-api-key-id"), "fixture-key")
        self.assertEqual(sent.get_header("Apca-api-secret-key"), "fixture-secret")
        self.assertNotIn("fixture", sent.full_url)
        query = parse_qs(urlsplit(sent.full_url).query)
        self.assertEqual(query["adjustment"], ["raw"])
        self.assertEqual(query["feed"], ["sip"])
        self.assertEqual(query["asof"], ["2025-03-10"])
        self.assertEqual(query["start"], ["2025-03-10T00:00:00-04:00"])
        self.assertEqual(query["end"], ["2025-03-10T23:59:59.999999-04:00"])
        self.assertEqual(self.files(), [self.client.minute_cache_path("XYZ", DAY)])
        self.assertEqual(self.files()[0].parent.name, "alpaca_sip")

    def test_previous_close_across_dst_weekend_uses_exact_regular_daily_bar(self):
        request = self.network(*self.calibration())
        close = self.client.previous_close("XYZ", DAY)
        self.assertEqual((close.trading_date, close.close, close.source_close, close.split_factor),
                         (PREVIOUS, 10, 10, 1))
        queries = [parse_qs(urlsplit(call.args[0].full_url).query) for call in request.call_args_list]
        self.assertEqual(queries[1]["start"], ["2025-03-07T00:00:00-05:00"])
        self.assertEqual(queries[1]["timeframe"], ["1Day"])
        self.assertEqual(queries[2]["adjustment"], ["split"])
        self.assertEqual(queries[4]["start"], ["2025-03-10T04:00:00-04:00"])
        self.assertEqual(queries[4]["end"], ["2025-03-10T04:00:59.999999-04:00"])

    def test_reverse_split_and_future_split_normalize_only_between_sessions(self):
        self.network(*self.calibration(split=10, future=0.2))
        close = self.client.previous_close("XYZ", DAY)
        self.assertAlmostEqual(close.close, 100)
        self.assertAlmostEqual(close.split_factor, 10)
        self.assertEqual(close.source_close, 10)
        self.assertEqual(self.client.minute_bars("XYZ", DAY)[0].close, 15)

    def test_future_splits_alone_do_not_change_historical_close(self):
        self.network(*self.calibration(future=0.25))
        close = self.client.previous_close("XYZ", DAY)
        self.assertEqual((close.close, close.source_close, close.split_factor), (10, 10, 1))

    def test_forward_split_and_holiday_previous_session(self):
        prior, day = date(2025, 7, 3), date(2025, 7, 7)
        self.network(*self.calibration(prior=prior, day=day, split=0.5, future=10))
        close = self.client.previous_close("XYZ", day)
        self.assertEqual(close.trading_date, prior)
        self.assertAlmostEqual(close.close, 5)
        self.assertAlmostEqual(close.split_factor, 0.5)

    def test_previous_close_and_minutes_replay_offline_without_credentials(self):
        self.network(*self.calibration(split=10, future=0.5))
        expected = self.client.previous_close("XYZ", DAY)
        offline = AlpacaClient(self.config, None, None, offline=True)
        with patch.object(offline._opener, "open", side_effect=AssertionError("network called")):
            self.assertEqual(offline.previous_close("XYZ", DAY), expected)
            self.assertEqual(len(offline.minute_bars("XYZ", DAY)), 1)
        # Calendar, raw minutes, and the atomic calibration; adjusted responses
        # must not be cached independently and mixed with later vendor vintages.
        self.assertEqual(len(self.files()), 3)
        for filename in self.files():
            self.assertNotIn("fixture-key", filename.read_text())
            self.assertNotIn("fixture-secret", filename.read_text())
        self.assertFalse(list(self.directory.rglob("*.tmp")))

    def test_missing_prior_close_never_uses_stale_session(self):
        payloads = self.calibration()
        payloads[1] = bars(rows=[])
        self.network(*payloads)
        with self.assertRaisesRegex(DataError, "raw prior"):
            self.client.previous_close("XYZ", DAY)
        self.assertFalse(any(json.loads(path.read_text())["request"]["path"] == "/backtest/previous-close"
                             for path in self.files()))

    def test_missing_target_minute_explicitly_rejects_split_calibration(self):
        payloads = self.calibration()
        payloads[3] = bars(rows=[])
        self.network(*payloads)
        with self.assertRaisesRegex(DataError, "No target-day SIP bar"):
            self.client.previous_close("XYZ", DAY)

    def test_unmatched_reference_minutes_reject_calibration(self):
        payloads = self.calibration()
        payloads[4] = bars(rows=[bar(clock="04:01", price=15)])
        self.network(*payloads)
        with self.assertRaisesRegex(DataError, "timestamps do not match"):
            self.client.previous_close("XYZ", DAY)

    def test_inconsistent_adjusted_reference_rejects_calibration(self):
        payloads = self.calibration()
        payloads[4]["bars"]["XYZ"][0]["h"] = 30
        self.network(*payloads)
        with self.assertRaisesRegex(DataError, "raw/split reference bars are inconsistent"):
            self.client.previous_close("XYZ", DAY)

    def test_paginated_results_remain_on_fixed_host_and_preserve_missing_minutes(self):
        request = self.network(bars(next_page_token="https://evil.example/?token=two"),
                               bars(rows=[bar(clock="04:03")]))
        result = self.client.minute_bars("XYZ", DAY)
        self.assertEqual([item.timestamp.minute for item in result], [0, 3])
        self.assertEqual(urlsplit(request.call_args.args[0].full_url).hostname, "data.alpaca.markets")
        self.assertNotIn("next_page_token", self.files()[0].read_text())

    def test_repeating_pagination_and_duplicate_timestamps_are_rejected(self):
        for second in (bars(next_page_token="two"), bars()):
            with self.subTest(second=second):
                with patch.object(self.client._opener, "open", side_effect=[
                        response(bars(next_page_token="two")), response(second)]):
                    with self.assertRaises(DataError):
                        self.client.minute_bars("XYZ", DAY)
                    self.assertFalse(self.files())

    def test_refresh_bypasses_cache_and_offline_cache_misses_are_explicit(self):
        with self.assertRaisesRegex(DataError, "Offline Alpaca cache miss"):
            AlpacaClient(self.config, None, None, offline=True).minute_bars("XYZ", DAY)
        self.network(bars())
        self.client.minute_bars("XYZ", DAY)
        fresh = AlpacaClient(self.config, "key", "secret", refresh_cache=True)
        with patch.object(fresh._opener, "open", return_value=response(bars(rows=[]))):
            self.assertEqual(fresh.minute_bars("XYZ", DAY), [])

    def test_identity_completeness_and_unfinished_pagination_are_validated_in_cache(self):
        self.network(bars())
        self.client.minute_bars("XYZ", DAY)
        path = self.files()[0]
        original = json.loads(path.read_text())
        for field, replacement in (("request", {}), ("version", 99), ("complete", False),
                                   ("pages", [bars(next_page_token="unfinished")])):
            with self.subTest(field=field):
                path.write_text(json.dumps({**original, field: replacement}))
                with self.assertRaisesRegex(DataError, "refresh-cache"):
                    self.client.minute_bars("XYZ", DAY)

    def test_current_date_is_never_cached_and_offline_rejects_it(self):
        today = datetime.now(ET).date()
        request = self.network(bars(rows=[bar(today)]), bars(rows=[]))
        self.assertEqual(len(self.client.minute_bars("XYZ", today)), 1)
        self.assertEqual(self.client.minute_bars("XYZ", today), [])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.files(), [])
        with self.assertRaisesRegex(DataError, "completed historical"):
            AlpacaClient(self.config, None, None, offline=True).minute_bars("XYZ", today)

    def test_current_date_prior_close_calibration_is_refetched_without_caching(self):
        today = datetime.now(ET).date()
        payloads = self.calibration(prior=today - timedelta(days=1), day=today)
        request = self.network(*payloads, *payloads[1:])
        first = self.client.previous_close("XYZ", today)
        self.assertEqual(self.client.previous_close("XYZ", today), first)
        self.assertEqual(request.call_count, 9)
        # Only the completed prior-session calendar can persist.
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(json.loads(self.files()[0].read_text())["request"]["params"]["symbols"], "SPY")
        offline = AlpacaClient(self.config, None, None, offline=True)
        with self.assertRaisesRegex(DataError, "completed historical"):
            offline.previous_close("XYZ", today)

    def test_invalid_payloads_cannot_be_cached_or_leak_secrets(self):
        invalid = [[], {}, {"code": 403, "message": "fixture-secret"}, bars("WRONG"),
                   bars(rows="invalid"), bars(rows=[{**bar(), "t": "2025-03-10T04:00:00"}]),
                   bars(rows=[bar(PREVIOUS)]), bars(rows=[{**bar(), "c": 50}]),
                   bars(rows=[{**bar(), "h": float("nan")}]),
                   bars(rows=[{**bar(), "v": True}]), bars(rows=[{**bar(), "o": "10"}])]
        for payload in invalid:
            with self.subTest(payload=payload):
                with patch.object(self.client._opener, "open", return_value=response(payload)):
                    with self.assertRaises(DataError) as error:
                        self.client.minute_bars("XYZ", DAY)
                    self.assertNotIn("fixture-secret", str(error.exception))
                    self.assertFalse(self.files())

    def test_empty_bars_null_is_valid_but_error_on_later_page_is_not(self):
        self.network({"bars": None, "next_page_token": None})
        self.assertEqual(self.client.minute_bars("XYZ", DAY), [])
        self.client.refresh_cache = True
        with patch.object(self.client._opener, "open", side_effect=[response(bars(next_page_token="two")),
                 response({"message": "fixture-secret", "code": 403})]):
            with self.assertRaisesRegex(DataError, "error response"):
                self.client.minute_bars("XYZ", DAY)
        self.assertEqual(AlpacaClient(self.config, None, None, offline=True).minute_bars("XYZ", DAY), [])

    def test_auth_and_redirect_errors_are_sanitized_without_retry(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))
        for status in (302, 401, 403, 422):
            with self.subTest(status=status):
                with patch.object(self.client._opener, "open", side_effect=HTTPError(
                        "https://data.alpaca.markets", status, "fixture-secret", {}, None)) as request:
                    with self.assertRaisesRegex(DataError, f"HTTP {status}") as error:
                        self.client.minute_bars("XYZ", DAY)
                    self.assertNotIn("fixture-secret", str(error.exception))
                    self.assertEqual(request.call_count, 1)

    def test_retryable_statuses_honor_retry_after(self):
        request = self.network(HTTPError("https://data.alpaca.markets", 429, "secret", {"Retry-After": "3"}, None),
                               HTTPError("https://data.alpaca.markets", 503, "secret", {}, None), bars())
        self.assertEqual(len(self.client.minute_bars("XYZ", DAY)), 1)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [3, 2])

    def test_network_failures_and_invalid_json_do_not_leak_secrets(self):
        for failure in (URLError("fixture-secret"),):
            with patch.object(self.client._opener, "open", side_effect=failure) as request:
                with self.assertRaisesRegex(DataError, "network request failed") as error:
                    self.client.minute_bars("XYZ", DAY)
                self.assertEqual(request.call_count, 3)
                self.assertNotIn("fixture-secret", str(error.exception))
        with patch.object(self.client._opener, "open", return_value=io.BytesIO(b"fixture-secret")):
            with self.assertRaisesRegex(DataError, "invalid JSON"):
                self.client.minute_bars("XYZ", DAY)

    def test_missing_credentials_invalid_headers_and_incompatible_flags(self):
        for key, secret in ((None, None), ("key", None), (None, "secret")):
            client = AlpacaClient(self.config, key, secret)
            with patch.object(client._opener, "open") as request:
                with self.assertRaisesRegex(DataError, "ALPACA_API_KEY.*ALPACA_SECRET_KEY"):
                    client.minute_bars("XYZ", DAY)
                request.assert_not_called()
        for credential in ("fixture-secret\r\nX:bad", "fixture-secret\x00", "fixture-secret\u0100"):
            with self.assertRaisesRegex(DataError, "Invalid Alpaca credential") as error:
                AlpacaClient(self.config, "key", credential)
            self.assertNotIn("fixture-secret", str(error.exception))
        with self.assertRaisesRegex(DataError, "cannot refresh"):
            AlpacaClient(self.config, None, None, offline=True, refresh_cache=True)


if __name__ == "__main__":
    unittest.main()
