"""Checks for the offline post-stop excursion diagnostic."""

import tempfile
import unittest
from dataclasses import asdict
from datetime import date
from pathlib import Path

from analyze_4am_short_excursions import at, describe, inspect_stop, summarize
from four_am_short.config import BacktestConfig, DataConfig, StrategyConfig
from four_am_short.models import Bar, DataError, PreviousClose
from four_am_short.strategy import simulate


class BarsClient:
    def __init__(self, bars=None, error=None):
        self.bars = bars
        self.error = error

    def minute_bars(self, symbol, day):
        if self.error:
            raise DataError(self.error)
        return self.bars


class ExcursionAnalysisTests(unittest.TestCase):
    day = date(2026, 1, 2)

    def bar(self, clock, opening, high, low, close):
        return Bar(at(self.day, clock), opening, high, low, close)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = BacktestConfig(
            Path(self.temp.name) / "input.csv", Path(self.temp.name),
            StrategyConfig(entry_deadline="09:00"),
            DataConfig(cache_dir=Path(self.temp.name)),
        )
        self.bars = [
            self.bar("04:00", 9.5, 10, 9, 9.5),
            self.bar("04:20", 9, 9, 8.5, 9),
            self.bar("04:21", 11.7, 30, 11.7, 12),
            self.bar("04:22", 12, 13, 11, 12),
            self.bar("08:59", 13, 14, 12, 13),
            self.bar("09:00", 50, 100, 40, 50),
            self.bar("09:20", 60, 110, 50, 60),
            self.bar("09:30", 90, 120, 50, 90),
        ]

    def source(self, bars):
        trade = simulate(self.day, "TEST", bars, PreviousClose(date(2025, 12, 31), 5), self.config.strategy)
        self.assertEqual(trade.exit_reason, "stop_loss")
        return {key: str(value) for key, value in asdict(trade).items()}

    def test_excludes_stop_minute_and_exact_cutoff_bars(self):
        row = inspect_stop(self.source(self.bars), self.config, BarsClient(self.bars), ["09:00", "09:20", "09:30"])
        self.assertEqual(row["analysis_status"], "ok")
        self.assertEqual(row["stop_bar_high"], 30)
        self.assertEqual(row["next_available_open"], 12)
        self.assertEqual(row["0900_maximum_high"], 14)
        self.assertEqual(row["0920_maximum_high"], 100)
        self.assertEqual(row["0930_maximum_high"], 110)
        self.assertEqual(row["0900_observed_bars"], 2)
        self.assertGreater(row["0900_unobserved_minutes"], 0)
        self.assertTrue(row["0900_exact_cutoff_bar_present"])

    def test_activation_at_deadline_has_no_eligible_future(self):
        bars = self.bars[:2] + [
            self.bar("08:59", 11.7, 12, 11.7, 12),
            self.bar("09:00", 13, 14, 12, 13),
            self.bar("09:20", 13, 15, 12, 13),
        ]
        row = inspect_stop(self.source(bars), self.config, BarsClient(bars), ["09:00", "09:20"])
        self.assertFalse(row["0900_activation_before_cutoff"])
        self.assertFalse(row["0900_next_open_eligible"])
        self.assertIsNone(row["0900_maximum_high"])
        stats = summarize([row], ["09:00", "09:20"], [7.5])["cutoffs"]
        self.assertEqual(stats["09:00"]["future_max_above_high_percent"], {"count": 0})
        self.assertEqual(stats["09:20"]["eligible_with_observed_post_stop_bar"], 1)
        self.assertEqual(stats["09:20"]["offset_reach"][0]["next_open_already_at_or_above"], 1)

    def test_missing_cache_and_mismatching_replay_excluded_not_zero_filled(self):
        source = self.source(self.bars)
        missing = inspect_stop(source, self.config, BarsClient(error="Offline cache miss"), ["09:00"])
        source["exit_price"] = "999"
        mismatch = inspect_stop(source, self.config, BarsClient(self.bars), ["09:00"])
        stats = summarize([missing, mismatch], ["09:00"], [7.5])
        self.assertEqual(stats["unavailable_cached_replays"], 2)
        self.assertEqual(stats["valid_cached_replays"], 0)
        self.assertEqual(stats["cutoffs"]["09:00"]["future_max_above_high_percent"], {"count": 0})
        self.assertIn("exit_price", mismatch["analysis_error"])

    def test_statistics_keep_outlier_visible(self):
        stats = describe([10, 20, 30, 1000])
        self.assertEqual(stats["mean"], 265)
        self.assertEqual(stats["median"], 25)
        self.assertEqual(stats["p25"], 17.5)
        self.assertEqual(stats["mean_excluding_single_largest"], 20)
        self.assertEqual(describe([]), {"count": 0})


if __name__ == "__main__":
    unittest.main()
