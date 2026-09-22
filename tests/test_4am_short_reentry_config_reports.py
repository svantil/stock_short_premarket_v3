"""Re-entry settings and multi-trade reports preserve stock/day setup counts."""

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from four_am_short.config import ReentryConfig, load_config
from four_am_short.live.config import load_live_settings
from four_am_short.models import DataError, TradeResult
from four_am_short.reports import (
    format_gap_summary, format_trade_summary, gap_not_traded_reasons,
    monthly_summaries, summarize, write_reports,
)


class ReentryConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "backtest.json"
        self.live_path = self.root / "live.json"
        self.raw = {"input_file": "stocks.csv", "shares": 500, "strategy": {"entry_deadline": "09:00"}}
        (self.root / "stocks.csv").write_text("2026-01-05 AAA\n")

    def load(self, reentry=None):
        if reentry is not None:
            self.raw["strategy"]["reentry"] = reentry
        self.path.write_text(json.dumps(self.raw))
        return load_config(self.path)

    def test_defaults_and_custom_rules_round_trip_in_snapshot(self):
        config = self.load()
        self.assertEqual(config.strategy.reentry, ReentryConfig())
        values = {"enabled": True, "entry_above_high_percent": 8, "stop_loss_percent": 15,
                  "profit_target_percent": 35, "entry_deadline": "09:10", "time_exit": "09:25"}
        config = self.load(values)
        self.assertEqual(config.strategy.shares, 500)
        self.assertEqual(config.strategy.entry_deadline, "09:00")
        self.assertEqual(config.snapshot()["strategy"]["reentry"], values)
        self.path.write_text(json.dumps(config.snapshot()))
        self.assertEqual(load_config(self.path).strategy, config.strategy)

    def test_invalid_reentry_options_rejected_even_when_disabled(self):
        invalid = [False, [], {"enabled": "true"}, {"enabled": 1}, {"enabled": None},
                   {"shares": 100}, {"max_reentries": 3},
                   {"entry_above_high_percent": -1}, {"entry_above_high_percent": True},
                   {"entry_above_high_percent": float("inf")},
                   {"stop_loss_percent": 0}, {"stop_loss_percent": "20"},
                   {"profit_target_percent": 100}, {"profit_target_percent": float("nan")},
                   {"time_exit": "9:20"}, {"entry_deadline": "09:21"},
                   {"entry_deadline": "04:15"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DataError):
                self.load(value)
        self.raw["strategy"]["reentry"] = None
        with self.assertRaises(DataError):
            self.load()

    def test_live_override_is_independent_and_inherits_other_rules(self):
        for shared in (False, True):
            self.load({"enabled": shared, "entry_above_high_percent": 7})
            for override in (False, True, None, "omitted"):
                with self.subTest(shared=shared, override=override):
                    execution = {} if override == "omitted" else {"reentry_enabled": override}
                    self.live_path.write_text(json.dumps({"strategy_config": "backtest.json", "execution": execution}))
                    settings = load_live_settings(self.live_path)
                    expected = shared if override in (None, "omitted") else override
                    self.assertEqual(settings.strategy.reentry.enabled, expected)
                    self.assertEqual(settings.strategy.reentry.entry_above_high_percent, 7)
                    self.assertEqual(settings.public_rules()["reentry"]["enabled"], expected)
                    self.assertEqual(settings.strategy.shares, 500)
                    self.assertEqual(load_config(self.path).strategy.reentry.enabled, shared)
        for value in ("true", 1, [], {}):
            with self.subTest(value=value):
                self.live_path.write_text(json.dumps({"strategy_config": "backtest.json", "execution": {"reentry_enabled": value}}))
                with self.assertRaises(ValueError):
                    load_live_settings(self.live_path)


class ReentryReportTests(unittest.TestCase):
    def rows(self):
        first = TradeResult(
            "2026-01-05", "AAA", "trade", first_gap_time="2026-01-05T04:00:00-05:00",
            early_high=20, entry_time="2026-01-05T04:16:00-05:00", entry_price=18,
            exit_time="2026-01-05T04:30:00-05:00", exit_reason="stop_loss",
            gross_pnl=-540, commission=2, locate_cost=5, net_pnl=-547,
        )
        second = replace(first, trade_number=2, entry_price=23.4,
                         entry_time="2026-01-05T04:31:00-05:00",
                         exit_time="2026-01-05T07:00:00-05:00", exit_reason="profit_target",
                         gross_pnl=936, commission=2, locate_cost=0, net_pnl=934)
        return [first, second]

    def test_two_trades_are_one_gap_setup_and_performance_includes_both(self):
        rows = self.rows()
        stats = summarize(rows)
        for name in ("candidates", "gap_triggered", "gap_traded", "gap_completed", "initial_trades",
                     "reentry_attempts", "reentry_trades", "stops", "targets"):
            self.assertEqual(stats[name], 1, name)
        self.assertEqual(stats["trades"], 2)
        self.assertEqual(stats["gap_not_traded"], 0)
        self.assertEqual(stats["net_pnl"], 387)
        self.assertEqual(stats["commission"], 4)
        self.assertEqual(stats["locate_cost"], 5)
        self.assertEqual(stats["average_net_pnl"], 193.5)
        self.assertEqual(monthly_summaries(rows)[0]["trades"], 2)
        self.assertIn("Re-entry attempts: 1; 1 completed", format_trade_summary(rows))
        stopped_again = replace(rows[1], exit_reason="stop_loss")
        self.assertEqual(summarize([rows[0], stopped_again])["stops"], 2)

    def test_unfilled_or_unresolved_reentry_does_not_change_first_entry_conversion(self):
        rows = self.rows()
        for status, price, reason in (("skipped", None, "entry_not_filled_before_deadline"),
                                      ("incomplete", 23.4, "missing_time_exit_bar")):
            with self.subTest(status=status):
                child = replace(rows[1], status=status, reason=reason, entry_price=price,
                                exit_time="", exit_reason="", gross_pnl=None, net_pnl=None)
                data = [rows[0], child]
                stats = summarize(data)
                self.assertEqual(stats["gap_traded"], 1)
                self.assertEqual(stats["gap_not_traded"], 0)
                self.assertEqual(stats["gap_unresolved"], 0)
                self.assertEqual(stats["trades"], 1)
                self.assertEqual(stats["net_pnl"], -547)
                self.assertEqual(stats[f"reentry_{status}"], 1)
                self.assertEqual(gap_not_traded_reasons(data), {})
                self.assertNotIn("Qualified but not traded:", format_gap_summary(data, load_defaults()))

    def test_saved_reports_count_candidates_once_and_preserve_trade_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stocks.csv").write_text("2026-01-05 AAA\n2026-01-06 BBB\n")
            config_path = root / "backtest.json"
            config_path.write_text(json.dumps({"input_file": "stocks.csv", "strategy": {"reentry": {"enabled": True}}}))
            output, saved = write_reports(load_config(config_path), self.rows(), requested_count=2, interrupted=True)
            self.assertEqual(saved["processed_candidates"], 1)
            self.assertEqual(saved["unprocessed_candidates"], 1)
            for filename in ("4am_short_candidates.csv", "4am_short_trades.csv"):
                with (output / filename).open() as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual([row["trade_number"] for row in rows], ["1", "2"])
            with (output / "4am_short_monthly.csv").open() as handle:
                month = next(csv.DictReader(handle))
            self.assertEqual(month["candidates"], "1")
            self.assertEqual(month["reentry_trades"], "1")
            self.assertEqual(month["trades"], "2")
            self.assertEqual(saved["statistics"]["reentry_trades"], 1)
            details = (output / "4am_short_trade_details.txt").read_text()
            self.assertNotIn("[2/2]", details)
            self.assertIn("Re-entry", details)


def load_defaults():
    from four_am_short.config import StrategyConfig
    return StrategyConfig()


if __name__ == "__main__":
    unittest.main()
