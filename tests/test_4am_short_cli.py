"""Exercise configuration -> provider -> strategy -> reports as a whole."""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from four_am_short.cli import main
from four_am_short.models import Bar, DataError, EASTERN, PreviousClose


class CLITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.input = self.root / "stocks.txt"
        self.original_input = b"2026-01-05 AAA BBB\n"
        self.input.write_bytes(self.original_input)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"input_file": "stocks.txt", "shares": 1000}))
        self.output = io.StringIO()

    def run_cli(self, *arguments):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return main(["--config", str(self.config), *arguments])

    def summary(self):
        path = next((self.root / "outcome").glob("4am_short/*/4am_short_summary.json"))
        return path.parent, json.loads(path.read_text())

    def bars(self):
        def bar(clock, opening, high, low, close):
            return Bar(datetime.fromisoformat(f"2026-01-05T{clock}:00").replace(tzinfo=EASTERN), opening, high, low, close)
        return [bar("04:00", 13.1, 14, 13.1, 13.5), bar("04:15", 13, 13.1, 11, 11.2)]

    def test_validate_does_not_require_credentials_or_network(self):
        with patch("four_am_short.cli.MassiveClient") as provider:
            self.assertEqual(self.run_cli("--validate-only"), 0)
            provider.assert_not_called()
        self.assertFalse((self.root / "outcome").exists())

    def test_end_to_end_fills_reports_and_preserves_original_input(self):
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.return_value = PreviousClose(date(2026, 1, 2), 10)
            def fetch(*args):
                self.input.write_text("2026-01-06 NEW\n")  # Concurrent scanner update.
                return self.bars()
            provider.return_value.minute_bars.side_effect = fetch
            self.assertEqual(self.run_cli("--offline"), 0)
        folder, summary = self.summary()
        self.assertEqual(summary["statistics"]["trades"], 2)
        self.assertEqual(summary["strategy_id"], "4am_short")
        self.assertEqual(summary["strategy_name"], "4am short")
        self.assertTrue(folder.name.startswith("4am_short_"))
        self.assertEqual(summary["statistics"]["targets"], 2)
        self.assertNotIn("reentry_statistics", summary)
        self.assertNotIn("reentry_monthly_statistics", summary)
        self.assertFalse((folder / "4am_short_reentry_monthly.csv").exists())
        self.assertAlmostEqual(summary["statistics"]["net_pnl"], 3250)
        self.assertEqual((folder / "4am_short_input.snapshot.txt").read_bytes(), self.original_input)
        with (folder / "4am_short_trades.csv").open(newline="") as handle:
            trades = list(csv.DictReader(handle))
        self.assertEqual(float(trades[0]["first_gap_bar_high"]), 14)
        self.assertEqual(trades[0]["early_high_bar_time"], "2026-01-05T04:00:00-05:00")
        self.assertEqual(trades[0]["profit_target_hit"], "True")
        self.assertEqual(trades[0]["strategy_name"], "4am short")
        details = (folder / "4am_short_trade_details.txt").read_text()
        self.assertIn("AAA", details)
        self.assertIn("BBB", details)
        self.assertIn("PROFIT TARGET", details)
        self.assertIn("PROFIT TARGET", self.output.getvalue())
        for heading in ("Gap-to-trade summary - 4am short", "Gap Triggers", "Not Traded", "Monthly summary - 4am short", "Wins", "Losses", "Loss%", "Avg/Trade", "Max DD", "Win Days", "Loss Days", "Stop Stocks"):
            self.assertIn(heading, details)
            self.assertIn(heading, self.output.getvalue())
        for text in (details, self.output.getvalue()):
            self.assertNotIn("Re-entry monthly summary - 4am short", text)
            self.assertNotIn("Max Stop Stocks", text)
            self.assertLess(text.index("Monthly summary - 4am short"), text.index("Trade summary - 4am short"))
            self.assertTrue(any(line.startswith("TOTAL") for line in text.splitlines()))
            final_summary = text[text.index("Trade summary - 4am short"):]
            for label in ("Avg winner", "Avg loser", "Biggest winner", "Biggest loser", "Avg trade", "Profit factor"):
                self.assertIn(label, final_summary)
            self.assertTrue(text.strip().splitlines()[-1].strip().startswith("Profit factor"))
            self.assertTrue(text.strip().splitlines()[-1].endswith("inf"))
        self.assertEqual(summary["statistics"]["average_winner_net_pnl"], 1625)
        self.assertEqual(summary["statistics"]["biggest_winner_net_pnl"], 1625)
        self.assertIsNone(summary["statistics"]["average_loser_net_pnl"])
        self.assertIsNone(summary["statistics"]["biggest_loser_net_pnl"])
        with (folder / "4am_short_monthly.csv").open(newline="") as handle:
            monthly = list(csv.DictReader(handle))
        self.assertEqual(monthly[0]["winning_days"], "1")
        self.assertEqual(monthly[0]["losing_days"], "0")
        self.assertEqual(monthly[0]["stops"], "0")
        self.assertNotIn("max_stop_stocks", monthly[0])
        self.assertEqual(monthly[0]["wins"], "2")
        self.assertEqual(monthly[0]["losses"], "0")
        self.assertEqual(float(monthly[0]["loss_percent"]), 0.0)
        self.assertEqual(summary["monthly_statistics"][0]["loss_percent"], 0.0)
        self.assertEqual(summary["statistics"]["gap_triggered"], 2)
        self.assertEqual(summary["statistics"]["gap_traded"], 2)
        self.assertEqual(summary["statistics"]["gap_not_traded"], 0)
        self.assertEqual(monthly[0]["gap_triggered"], "2")
        self.assertEqual(monthly[0]["gap_traded"], "2")
        self.assertEqual(monthly[0]["gap_not_traded"], "0")
        self.assertIn("Gap-to-trade summary - 4am short", (folder / "4am_short_gap_summary.txt").read_text())

    def test_candidate_data_error_persists_report_and_nonzero_exit(self):
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.side_effect = DataError("missing prior close")
            self.assertEqual(self.run_cli("--offline"), 3)
        self.assertEqual(self.summary()[1]["statistics"]["errors"], 2)

    def test_reentry_exports_two_trades_without_duplicating_candidate_progress(self):
        self.input.write_text("2026-01-05 AAA\n")
        self.config.write_text(json.dumps({
            "input_file": "stocks.txt", "shares": 1000,
            "strategy": {"reentry": {"enabled": True}},
        }))
        history = [
            Bar(datetime.fromisoformat(f"2026-01-05T{clock}:00").replace(tzinfo=EASTERN), price, price, price, price)
            for clock, price in (("04:00", 20), ("04:15", 18), ("04:16", 24), ("04:17", 24), ("04:18", 14))
        ]
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.return_value = PreviousClose(date(2026, 1, 2), 10)
            provider.return_value.minute_bars.return_value = history
            self.assertEqual(self.run_cli("--offline"), 0)
        folder, summary = self.summary()
        self.assertEqual(summary["processed_candidates"], 1)
        self.assertEqual(summary["unprocessed_candidates"], 0)
        self.assertEqual(summary["statistics"]["trades"], 2)
        self.assertEqual(summary["statistics"]["gap_triggered"], 1)
        self.assertEqual(summary["statistics"]["gap_traded"], 1)
        with (folder / "4am_short_trades.csv").open(newline="") as handle:
            trades = list(csv.DictReader(handle))
        self.assertEqual([row["trade_number"] for row in trades], ["1", "2"])
        self.assertEqual(summary["reentry_statistics"]["trades"], 1)
        self.assertEqual(summary["reentry_statistics"]["initial_trades"], 0)
        self.assertEqual(summary["reentry_statistics"]["targets"], 1)
        self.assertEqual(summary["reentry_statistics"]["stops"], 0)
        self.assertEqual(summary["reentry_statistics"]["net_pnl"], float(trades[1]["net_pnl"]))
        self.assertEqual(summary["reentry_monthly_statistics"][0]["trades"], 1)
        self.assertEqual(summary["monthly_statistics"][0]["trades"], 2)
        with (folder / "4am_short_reentry_monthly.csv").open(newline="") as handle:
            reentry_months = list(csv.DictReader(handle))
        self.assertEqual(len(reentry_months), 1)
        self.assertEqual(reentry_months[0]["period"], "2026-01")
        self.assertEqual(reentry_months[0]["trades"], "1")
        self.assertEqual(reentry_months[0]["initial_trades"], "0")
        self.assertEqual(float(reentry_months[0]["net_pnl"]), float(trades[1]["net_pnl"]))
        details = (folder / "4am_short_trade_details.txt").read_text()
        for text in (details, self.output.getvalue()):
            self.assertLess(text.index("Monthly summary - 4am short"), text.index("Re-entry monthly summary - 4am short"))
            self.assertLess(text.index("Re-entry monthly summary - 4am short"), text.index("Trade summary - 4am short"))
            self.assertTrue(text.strip().splitlines()[-1].strip().startswith("Profit factor"))
        self.assertEqual(self.output.getvalue().count("[1/1] 2026-01-05 AAA"), 2)
        self.assertIn("Re-entry (trade 2)", self.output.getvalue())
        self.assertNotIn("[2/1]", self.output.getvalue())

    def test_enabled_reentry_exports_zero_month_when_initial_trades_hit_targets(self):
        self.config.write_text(json.dumps({
            "input_file": "stocks.txt", "shares": 1000,
            "strategy": {"reentry": {"enabled": True}},
        }))
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.return_value = PreviousClose(date(2026, 1, 2), 10)
            provider.return_value.minute_bars.return_value = self.bars()
            self.assertEqual(self.run_cli("--offline"), 0)
        folder, summary = self.summary()
        self.assertEqual(summary["statistics"]["trades"], 2)
        self.assertEqual(summary["reentry_statistics"]["trades"], 0)
        self.assertEqual(summary["reentry_statistics"]["net_pnl"], 0)
        self.assertEqual([row["period"] for row in summary["reentry_monthly_statistics"]], ["2026-01"])
        self.assertEqual(summary["reentry_monthly_statistics"][0]["trades"], 0)
        with (folder / "4am_short_reentry_monthly.csv").open(newline="") as handle:
            months = list(csv.DictReader(handle))
        self.assertEqual([(row["period"], row["trades"]) for row in months], [("2026-01", "0")])
        for text in ((folder / "4am_short_trade_details.txt").read_text(), self.output.getvalue()):
            self.assertIn("Re-entry monthly summary - 4am short", text)

    def test_enabled_reentry_interruption_marks_separate_summary_partial(self):
        self.config.write_text(json.dumps({
            "input_file": "stocks.txt", "strategy": {"reentry": {"enabled": True}},
        }))
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.side_effect = [PreviousClose(date(2026, 1, 2), 10), KeyboardInterrupt()]
            provider.return_value.minute_bars.return_value = self.bars()
            self.assertEqual(self.run_cli("--offline"), 130)
        folder, summary = self.summary()
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["processed_candidates"], 1)
        self.assertEqual(summary["reentry_statistics"]["trades"], 0)
        for text in ((folder / "4am_short_trade_details.txt").read_text(), self.output.getvalue()):
            self.assertIn("Re-entry monthly summary - 4am short (PARTIAL)", text)

    def test_session_completion_waits_for_later_enabled_reentry_cutoff(self):
        self.input.write_text("2026-01-05 AAA\n")
        self.config.write_text(json.dumps({
            "input_file": "stocks.txt",
            "strategy": {"reentry": {"enabled": True, "time_exit": "09:40"}},
        }))

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 5, 9, 35, tzinfo=EASTERN).astimezone(tz)

        with patch("four_am_short.cli.datetime", Clock), patch("four_am_short.cli.MassiveClient") as provider:
            self.assertEqual(self.run_cli("--offline"), 3)
            provider.return_value.previous_close.assert_not_called()
            provider.return_value.minute_bars.assert_not_called()
        self.assertEqual(self.summary()[1]["statistics"]["incomplete"], 1)

    def test_interruption_saves_partial_report(self):
        with patch("four_am_short.cli.MassiveClient") as provider:
            provider.return_value.previous_close.side_effect = [PreviousClose(date(2026, 1, 2), 10), KeyboardInterrupt()]
            provider.return_value.minute_bars.return_value = self.bars()
            self.assertEqual(self.run_cli("--offline"), 130)
        folder, summary = self.summary()
        self.assertTrue(summary["interrupted"])
        self.assertEqual(summary["processed_candidates"], 1)
        self.assertEqual(summary["unprocessed_candidates"], 1)
        self.assertEqual(summary["statistics"]["average_winner_net_pnl"], 1625)
        details = (folder / "4am_short_trade_details.txt").read_text()
        for text in (details, self.output.getvalue()):
            self.assertIn("Trade summary - 4am short (PARTIAL)", text)
            self.assertTrue(text.strip().splitlines()[-1].strip().startswith("Profit factor"))

    def test_future_date_never_requests_prices_or_reports_zero_pnl_trade(self):
        self.input.write_text("2999-01-05 AAA\n")
        with patch("four_am_short.cli.MassiveClient") as provider:
            self.assertEqual(self.run_cli("--offline"), 3)
            provider.return_value.previous_close.assert_not_called()
        self.assertEqual(self.summary()[1]["statistics"]["incomplete"], 1)

    def test_invalid_utf8_is_clean_input_error(self):
        self.input.write_bytes(b"\xff")
        self.assertEqual(self.run_cli("--validate-only"), 2)


if __name__ == "__main__":
    unittest.main()
