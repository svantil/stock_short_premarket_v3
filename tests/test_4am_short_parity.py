"""Offline comparisons preserve evidence and never contact a broker or network."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import csv
from dataclasses import asdict, replace
from datetime import date, datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import compare_4am_short_live as cli
from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.live.config import DataSettings, LiveSettings
from four_am_short.live.data import AlpacaData
from four_am_short.models import Bar, DataError, EASTERN
from four_am_short.parity import (
    compare, context_for_symbol, encode_bar, normalized,
    render, report_for_day, request_window, simulate_dataset,
)


DAY = date(2026, 1, 5)
RULES = StrategyConfig(shares=10, entry_deadline="09:15", time_exit="09:20",
                       reentry=ReentryConfig(enabled=True))


def bar(clock, price):
    return Bar(datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
               price, price, price, price, 100)


def context(rules=RULES):
    return {"strategy": asdict(rules), "strategy_source": "recorded live entry strategy",
            "previous_close": 2, "previous_close_source": "saved live close",
            "previous_close_date": "2026-01-02", "previous_close_date_source": "live evidence"}


def dataset():
    return {"schema": 1, "feed": "alpaca_sip", "date": str(DAY),
            "symbols": {"TEST": {**context(), "bars": [encode_bar(item) for item in (
                bar("04:00", 4), bar("04:15", 3.6), bar("04:16", 5),
                bar("04:17", 4.2), bar("09:20", 4),
            )]}}}


def write_report(directory, rows, *, modified):
    directory.mkdir(parents=True)
    path = directory / "4am_short_candidates.csv"
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    (directory / "4am_short_config.resolved.json").write_text(json.dumps({
        "shares": 10, "strategy": {key: value for key, value in asdict(RULES).items() if key != "shares"},
    }))
    os.utime(path, (modified, modified))
    return path


class ParityDataTests(unittest.TestCase):
    def test_latest_report_must_contain_requested_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_report(root / "old", [{"date": str(DAY), "symbol": "OLD"}], modified=100)
            write_report(root / "matching", [{"date": str(DAY), "symbol": "TEST"},
                                               {"date": "2026-01-06", "symbol": "OTHER"}], modified=200)
            write_report(root / "newest", [{"date": "2026-01-06", "symbol": "WRONG"}], modified=300)
            selected, rows, rules = report_for_day(root, DAY)
            self.assertEqual(selected, root / "matching")
            self.assertEqual(rows, [{"date": str(DAY), "symbol": "TEST"}])
            self.assertEqual(rules, asdict(RULES))
            with self.assertRaisesRegex(DataError, "No backtest rows"):
                report_for_day(root, DAY, root / "newest")

    def test_missing_day_returns_no_unrelated_backtest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_report(root / "other", [{"date": "2026-01-06", "symbol": "TEST"}], modified=100)
            self.assertEqual(report_for_day(root, DAY), (None, [], {}))

    def test_candidate_report_retains_skipped_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_report(root / "run", [{"date": str(DAY), "symbol": "SKIP", "status": "skipped",
                                          "reason": "entry_price_out_of_range"}], modified=100)
            _, rows, _ = report_for_day(root, DAY)
            self.assertEqual(rows[0]["symbol"], "SKIP")
            self.assertEqual(rows[0]["reason"], "entry_price_out_of_range")

    def test_entry_strategy_and_prior_close_override_current_settings(self):
        saved_rules = replace(RULES, shares=4, stop_loss_percent=22)
        state = {"candidates": {"TEST": {"previous_close": 99}}, "trades": {
            "TEST": {"symbol": "TEST", "entry_evidence": {
                "strategy": asdict(saved_rules), "previous_close": 2,
                "previous_close_date": "2026-01-02",
            }},
        }, "audit": {"strategy": asdict(RULES), "previous_close_date": "2025-12-31"}}
        actual = context_for_symbol(state, "TEST", replace(RULES, shares=500), [])
        self.assertEqual(actual["strategy"], asdict(saved_rules))
        self.assertEqual(actual["strategy_source"], "recorded live entry strategy")
        self.assertEqual(actual["previous_close"], 2)
        self.assertEqual(actual["previous_close_date"], "2026-01-02")
        self.assertEqual(actual["previous_close_date_source"], "live evidence")

    def test_deferred_retry_uses_latest_attempt_strategy_close_and_setup(self):
        first_rules = replace(RULES, shares=4, stop_loss_percent=22)
        latest_rules = replace(RULES, shares=7, stop_loss_percent=25)
        first = {"strategy": asdict(first_rules), "previous_close": 2,
                 "previous_close_date": "2026-01-02", "setup": {"entry_limit": 3.6}}
        latest = {"strategy": asdict(latest_rules), "previous_close": 3,
                  "previous_close_date": "2026-01-02", "setup": {"entry_limit": 4.5}}
        trade = {"symbol": "TEST", "entry_evidence": first,
                 "entry_attempt_evidence": [first, latest]}
        state = {"candidates": {"TEST": {"previous_close": 99, "entry_limit": 99}},
                 "trades": {"TEST": trade}}
        before = deepcopy(state)
        actual = context_for_symbol(state, "TEST", RULES, [])
        self.assertEqual(actual["strategy"], asdict(latest_rules))
        self.assertEqual(actual["previous_close"], 3)
        self.assertEqual(actual["previous_close_source"], "saved live close")
        report = compare(DAY, ["TEST"], state, [{"symbol": "TEST", "entry_limit": "4.5"}],
                         asdict(latest_rules), {"TEST": actual}, {}, {})
        self.assertFalse(any("Live setup differs" in note for note in report["symbols"]["TEST"]["notes"]))
        self.assertEqual(state, before)

    def test_legacy_close_date_inferred_only_when_backtest_close_matches(self):
        state = {"candidates": {"TEST": {"previous_close": 2}}}
        rows = [{"symbol": "TEST", "trade_number": "1", "previous_close": "2",
                 "previous_close_date": "2026-01-02"}]
        actual = context_for_symbol(state, "TEST", RULES, rows)
        self.assertIn("historical settings unverified", actual["strategy_source"])
        self.assertEqual(actual["previous_close_date_source"], "matching backtest prior-session date")
        different = context_for_symbol(state, "TEST", RULES, [{**rows[0], "previous_close": "3"}])
        self.assertIsNone(different["previous_close_date"])
        self.assertEqual(different["previous_close"], 2)
        fallback = context_for_symbol({}, "TEST", RULES, rows)
        self.assertEqual(fallback["previous_close"], 2)
        self.assertEqual(fallback["previous_close_source"], "saved backtest close; live close unavailable")

    def test_saved_dataset_rejects_wrong_schema_feed_and_day(self):
        for change in ({"schema": 2}, {"feed": "iex"}, {"date": "2026-01-06"}):
            with self.subTest(change=change), self.assertRaises(DataError):
                simulate_dataset({**dataset(), **change}, DAY, ["TEST"])

    def test_saved_strategy_is_validated_before_simulation(self):
        for change in ({"shares": 0}, {"min_entry_price": 10, "max_entry_price": 1},
                       {"entry_deadline": "25:00"}, {"stop_loss_percent": float("nan")}):
            with self.subTest(change=change):
                saved = dataset()
                saved["symbols"]["TEST"]["strategy"].update(change)
                results, errors = simulate_dataset(saved, DAY, ["TEST"])
                self.assertEqual(results, {})
                self.assertIn("TEST", errors)

    def test_invalid_saved_bars_never_produce_a_trade(self):
        valid = dataset()["symbols"]["TEST"]["bars"]
        invalid_rows = [
            [{**valid[0], "timestamp": "2026-01-06T04:00:00-05:00"}],
            [{**valid[0], "timestamp": "2026-01-05T04:00:01-05:00"}],
            [{**valid[0], "timestamp": "2026-01-05T04:00:00"}],
            [{**valid[0], "open": True}], [{**valid[0], "high": 3}],
            [{**valid[0], "low": 5}], [{**valid[0], "close": float("nan")}],
            [{**valid[0], "volume": -1}], [valid[0], valid[0]], [],
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows):
                saved = dataset()
                saved["symbols"]["TEST"]["bars"] = rows
                result, errors = simulate_dataset(saved, DAY, ["TEST"])
                self.assertEqual(result, {})
                self.assertIn("TEST", errors)

    def test_malformed_bar_timestamp_is_reported_as_symbol_error(self):
        for timestamp in (123, None, []):
            with self.subTest(timestamp=timestamp):
                saved = dataset()
                saved["symbols"]["TEST"]["bars"][0]["timestamp"] = timestamp
                result, errors = simulate_dataset(saved, DAY, ["TEST"])
                self.assertEqual(result, {})
                self.assertIn("TEST", errors)

    def test_invalid_prior_close_and_missing_symbols_are_reported(self):
        for changes in ({"previous_close_date": str(DAY)}, {"previous_close": 0},
                        {"previous_close": True}, {"previous_close": float("inf")}):
            with self.subTest(changes=changes):
                saved = dataset()
                saved["symbols"]["TEST"].update(changes)
                result, errors = simulate_dataset(saved, DAY, ["TEST", "MISSING"])
                self.assertEqual(result, {})
                self.assertEqual(set(errors), {"TEST", "MISSING"})

    def test_request_window_includes_final_exit_bar_for_both_attempts(self):
        later_reentry = replace(RULES, early_start="03:55", time_exit="09:20",
                                reentry=replace(RULES.reentry, time_exit="09:31"))
        self.assertEqual(request_window(DAY, {"A": context(), "B": context(later_reentry)}),
                         ("03:55", "09:32"))
        disabled = replace(later_reentry, reentry=replace(later_reentry.reentry, enabled=False))
        self.assertEqual(request_window(DAY, {"A": context(disabled)}), ("03:55", "09:21"))

    def test_dataset_rerun_is_deterministic_and_preserves_inputs(self):
        saved = dataset()
        before = deepcopy(saved)
        first, errors = simulate_dataset(saved, DAY, ["TEST"])
        second, later_errors = simulate_dataset(json.loads(json.dumps(saved)), DAY, ["TEST"])
        self.assertEqual(errors, {})
        self.assertEqual(later_errors, {})
        self.assertEqual(first, second)
        self.assertEqual(saved, before)
        self.assertEqual([r["trade_number"] for r in first["TEST"]], [1, 2])
        self.assertEqual(first["TEST"][1]["exit_time"], "2026-01-05T09:20:00-05:00")
        self.assertEqual(first["TEST"][1]["exit_reason"], "time_exit")

    def test_live_partial_fills_and_reentry_keep_recorded_gross_and_locate_costs(self):
        initial = {"symbol": "TEST", "date": str(DAY), "trade_number": 1, "status": "closed",
                   "entry_avg_price": 3.6, "exit_avg_price": 5, "entry_filled_qty": 4,
                   "requested_qty": 10, "realized_pnl": -5.6,
                   "locate": {"status": "available", "comparisons": [
                       {"selected": True, "actual_total_cost": 0.12}]}}
        retry = {**initial, "trade_number": 2, "entry_avg_price": 4.2,
                 "exit_avg_price": 4, "entry_filled_qty": 3, "realized_pnl": 0.6,
                 "locate": {**initial["locate"], "status": "reused"}}
        report = compare(DAY, ["TEST"], {"trades": {"TEST:reentry": retry, "TEST": initial}},
                         [], asdict(RULES), {"TEST": context()}, {}, {})
        live = report["symbols"]["TEST"]["live"]
        self.assertEqual([row["trade_number"] for row in live], [1, 2])
        self.assertEqual([row["shares"] for row in live], [4, 3])
        self.assertEqual([row["gross_pnl"] for row in live], [-5.6, 0.6])
        self.assertEqual([row["recorded_locate_cost"] for row in live], [0.12, 0])
        text = render(report)
        self.assertIn("| Live / 1 | 4 |", text)
        self.assertIn("| Live / 2 | 3 |", text)
        self.assertIn("$-5.6", text)
        unfinished = normalized({**initial, "status": "open", "realized_pnl": None}, live=True)
        self.assertIsNone(unfinished["gross_pnl"])


class ParityCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "backtest.json"
        self.config_path.write_text(json.dumps({"input_file": "unused.csv", "output_dir": "reports"}))
        self.state_path = self.root / "live.json"
        self.state_path.write_text(json.dumps({"days": {str(DAY): {
            "candidates": {"TEST": {"previous_close": 99}}, "trades": {},
        }}}))
        self.settings = LiveSettings(strategy=replace(RULES, shares=500),
                                     strategy_config_path=self.config_path)
        self.arguments = ["--date", str(DAY), "--live-state", str(self.state_path),
                          "--output-dir", str(self.root / "out")]

    def run_cli(self, extra=()):
        with patch.object(cli, "load_live_settings", return_value=self.settings), \
                patch.object(cli, "AlpacaData", side_effect=AssertionError("Network client is forbidden")) as client, \
                patch.object(cli, "fetch_sip", side_effect=AssertionError("Fetch is forbidden")) as fetch, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = cli.main(self.arguments + list(extra))
        client.assert_not_called()
        fetch.assert_not_called()
        return result

    def test_default_cli_is_offline_and_marks_unrequested_sip_simulation(self):
        self.assertEqual(self.run_cli(), 0)
        reports = list((self.root / "out").glob("*/comparison.json"))
        self.assertEqual(len(reports), 1)
        result = json.loads(reports[0].read_text())
        self.assertEqual(result["symbols"]["TEST"]["sip_historical"], [])
        self.assertIn("SIP simulation not requested", result["notes"][0])
        self.assertIsNone(result["sources"]["backtest_directory"])

    def test_saved_sip_replay_uses_its_original_context_after_config_changes(self):
        saved_path = self.root / "sip_data.json"
        saved_path.write_text(json.dumps(dataset()))
        self.assertEqual(self.run_cli(["--sip-data", str(saved_path)]), 0)
        result = json.loads(next((self.root / "out").glob("*/comparison.json")).read_text())
        actual = result["symbols"]["TEST"]
        self.assertEqual(actual["simulation_context"], context())
        self.assertEqual(actual["sip_historical"][0]["shares"], 10)
        self.assertEqual(actual["sip_historical"][0]["previous_close"], 2)
        self.assertEqual(result["sources"]["sip_dataset"], str(saved_path))
        self.assertEqual(len(result["simulator_sha256"]), 64)
        self.assertIn("fingerprint", result["notes"][0])

    def test_wrong_feed_saved_sip_fails_before_writing_comparison(self):
        saved_path = self.root / "sip_data.json"
        saved_path.write_text(json.dumps({**dataset(), "feed": "iex"}))
        self.assertEqual(self.run_cli(["--sip-data", str(saved_path)]), 2)
        self.assertFalse((self.root / "out").exists())

    def test_empty_saved_sip_is_rejected_instead_of_treated_as_unrequested(self):
        saved_path = self.root / "sip_data.json"
        for contents in ("{}", "null", "[]"):
            with self.subTest(contents=contents):
                saved_path.write_text(contents)
                self.assertEqual(self.run_cli(["--sip-data", str(saved_path)]), 2)
                self.assertFalse((self.root / "out").exists())

    def test_invalid_symbol_bars_write_diagnostics_without_inventing_trades(self):
        saved_path = self.root / "sip_data.json"
        saved = dataset()
        saved["symbols"]["TEST"]["bars"] = []
        saved_path.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli(["--sip-data", str(saved_path)]), 3)
        result = json.loads(next((self.root / "out").glob("*/comparison.json")).read_text())
        self.assertIn("TEST", result["simulation_errors"])
        self.assertEqual(result["symbols"]["TEST"]["sip_historical"], [])


class ParityFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_uses_only_get_and_retains_exact_time_exit_bar(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            if request.url.path == "/v2/calendar":
                return httpx.Response(200, json=[{"date": "2026-01-02"}, {"date": str(DAY)}])
            self.assertEqual(request.url.path, "/v2/stocks/bars")
            self.assertEqual(request.url.params["feed"], "sip")
            self.assertEqual(request.url.params["adjustment"], "raw")
            self.assertEqual(request.url.params["timeframe"], "1Min")
            self.assertEqual(request.url.params["asof"], str(DAY))
            self.assertEqual(request.url.params["end"], "2026-01-05T09:20:59.999999-05:00")
            bars = [{"t": item["timestamp"], "o": item["open"], "h": item["high"],
                     "l": item["low"], "c": item["close"], "v": item["volume"]}
                    for item in dataset()["symbols"]["TEST"]["bars"]]
            return httpx.Response(200, json={"bars": {"TEST": bars}, "next_page_token": None})

        data_settings = DataSettings(api_key="test-only", secret_key="test-only")
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        data = AlpacaData(data_settings, client=http, now=lambda: bar("10:00", 1).timestamp)
        saved_context = context()
        saved_context.update(previous_close_date=None, previous_close_date_source="unavailable")
        contexts = {"TEST": saved_context}
        with patch.object(cli, "AlpacaData", return_value=data):
            fetched = await cli.fetch_sip(LiveSettings(data=data_settings), DAY, contexts)
        self.assertTrue(http.is_closed)
        self.assertEqual([r.url.path for r in requests], ["/v2/calendar", "/v2/stocks/bars"])
        self.assertEqual(fetched["end_exclusive"], "09:21")
        self.assertEqual(fetched["symbols"]["TEST"]["previous_close"], 2)
        self.assertEqual(fetched["symbols"]["TEST"]["previous_close_date_source"], "Alpaca exchange calendar")
        rows, errors = simulate_dataset(fetched, DAY, ["TEST"])
        self.assertEqual(errors, {})
        self.assertEqual(rows["TEST"][1]["exit_reason"], "time_exit")
        self.assertEqual(rows["TEST"][1]["exit_time"], "2026-01-05T09:20:00-05:00")


if __name__ == "__main__":
    unittest.main()
