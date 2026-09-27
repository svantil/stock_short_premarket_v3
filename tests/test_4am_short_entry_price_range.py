"""Entry bounds apply to initial trades and re-entries without capping fills."""

from dataclasses import replace
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest

from four_am_short.config import ReentryConfig, StrategyConfig, load_config
from four_am_short.models import Bar, DataError, EASTERN, PreviousClose
from four_am_short.reentry_sweep_sim import prepare_case, simulate_prepared
from four_am_short.strategy import simulate, simulate_trades


DAY = date(2026, 1, 5)
PREVIOUS = PreviousClose(date(2026, 1, 2), 4)


def bar(clock, opening, high=None, low=None, close=None):
    return Bar(
        datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
        opening, opening if high is None else high,
        opening if low is None else low, opening if close is None else close,
    )


class EntryPriceConfigTests(unittest.TestCase):
    def load_strategy(self, strategy):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"input_file": "stocks.csv", "strategy": strategy}))
            return load_config(path)

    def test_legacy_and_null_bounds_remain_unrestricted(self):
        for values in ({}, {"min_entry_price": None, "max_entry_price": None}):
            config = self.load_strategy(values).strategy
            self.assertIsNone(config.min_entry_price)
            self.assertIsNone(config.max_entry_price)
            self.assertTrue(config.entry_price_allowed(0.01))
            self.assertTrue(config.entry_price_allowed(100))

    def test_inclusive_bounds_are_loaded_and_snapshotted(self):
        config = self.load_strategy({"min_entry_price": 1, "max_entry_price": 10})
        for price, expected in ((0.99, False), (1, True), (10, True), (10.01, False)):
            with self.subTest(price=price):
                self.assertEqual(config.strategy.entry_price_allowed(price), expected)
        self.assertEqual(config.snapshot()["strategy"]["min_entry_price"], 1)
        self.assertEqual(config.snapshot()["strategy"]["max_entry_price"], 10)
        exact = self.load_strategy({"min_entry_price": 1, "max_entry_price": 1}).strategy
        self.assertTrue(exact.entry_price_allowed(1))

    def test_one_sided_bounds(self):
        minimum = self.load_strategy({"min_entry_price": 1}).strategy
        maximum = self.load_strategy({"max_entry_price": 10}).strategy
        self.assertFalse(minimum.entry_price_allowed(0.99))
        self.assertTrue(minimum.entry_price_allowed(100))
        self.assertTrue(maximum.entry_price_allowed(0.01))
        self.assertFalse(maximum.entry_price_allowed(10.01))

    def test_invalid_bounds_and_reversed_range_are_rejected(self):
        for key in ("min_entry_price", "max_entry_price"):
            for value in (True, "1", 0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(key=key, value=value), self.assertRaises(DataError):
                    self.load_strategy({key: value})
        with self.assertRaisesRegex(DataError, "min_entry_price <="):
            self.load_strategy({"min_entry_price": 10, "max_entry_price": 1})

    def test_invalid_observed_prices_are_ineligible(self):
        config = StrategyConfig()
        for price in (True, None, 0, -1, float("nan"), float("inf")):
            with self.subTest(price=price):
                self.assertFalse(config.entry_price_allowed(price))


class EntryPriceSimulationTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(min_entry_price=1, max_entry_price=10)

    def test_initial_planned_limits_and_fills_include_both_boundaries(self):
        config = replace(self.config, entry_below_high_percent=0)
        for price in (1, 10):
            with self.subTest(price=price):
                result = simulate(
                    DAY, "TEST", [bar("04:00", price), bar("04:15", price), bar("09:30", price)],
                    replace(PREVIOUS, close=price / 2), config,
                )
                self.assertEqual(result.status, "trade")
                self.assertEqual(result.entry_limit, price)
                self.assertEqual(result.entry_price, price)

    def test_out_of_range_planned_limits_are_skipped_even_if_open_is_eligible(self):
        config = replace(self.config, entry_below_high_percent=0)
        for price in (0.99, 10.01):
            with self.subTest(price=price):
                result = simulate(
                    DAY, "TEST", [bar("04:00", price), bar("04:15", 10), bar("09:30", 9)],
                    replace(PREVIOUS, close=price / 2), config,
                )
                self.assertEqual(result.reason, "entry_price_out_of_range")
                self.assertIsNone(result.entry_price)

    def test_out_of_range_open_never_becomes_a_capped_or_same_bar_fill(self):
        for policy in ("conservative", "ohlc", "olhc"):
            with self.subTest(policy=policy):
                result = simulate(
                    DAY, "TEST", [bar("04:00", 10), bar("04:15", 11, 12, 8, 9), bar("09:30", 9)],
                    PREVIOUS, replace(self.config, intrabar_policy=policy),
                )
                self.assertEqual(result.entry_limit, 9)
                self.assertEqual(result.reason, "entry_price_out_of_range")
                self.assertEqual(result.status, "skipped")
                self.assertIsNone(result.entry_price)

    def test_pending_order_can_fill_later_when_open_returns_to_range(self):
        result = simulate(
            DAY, "TEST", [bar("04:00", 10), bar("04:15", 11), bar("04:16", 10), bar("09:30", 9)],
            PREVIOUS, self.config,
        )
        self.assertEqual(result.entry_price, 10)
        self.assertEqual(result.entry_time, "2026-01-05T04:16:00-05:00")
        self.assertEqual(result.status, "trade")

    def test_price_recovery_at_deadline_does_not_fill(self):
        result = simulate(
            DAY, "TEST", [bar("04:00", 10), bar("05:59", 11), bar("06:00", 10), bar("09:30", 9)],
            PREVIOUS, self.config,
        )
        self.assertEqual(result.reason, "entry_price_out_of_range")
        self.assertIsNone(result.entry_price)

    def test_intrabar_high_above_max_does_not_discard_valid_entry_or_stop(self):
        result = simulate(
            DAY, "TEST", [bar("04:00", 10), bar("04:15", 8, 12, 8, 11)],
            PREVIOUS, self.config,
        )
        self.assertEqual(result.entry_price, 9)
        self.assertEqual(result.exit_reason, "stop_loss")
        self.assertEqual(result.exit_price, 11.7)

    def test_existing_position_can_exit_below_minimum(self):
        result = simulate(
            DAY, "TEST", [bar("04:00", 1), bar("04:15", 1), bar("04:16", 0.5)],
            replace(PREVIOUS, close=0.5), replace(self.config, entry_below_high_percent=0),
        )
        self.assertEqual(result.entry_price, 1)
        self.assertEqual(result.exit_reason, "profit_target")
        self.assertEqual(result.exit_price, 0.875)

    def test_reentry_and_sweep_apply_bounds_and_wait_for_later_eligible_bars(self):
        stopped = [bar("04:00", 8), bar("04:15", 7.2), bar("04:16", 7.2, 10, 7, 9.5)]
        cases = (
            (25, [bar("04:17", 10), bar("09:20", 9)], 10, ""),
            (30, [bar("04:17", 11), bar("09:20", 9)], None, "entry_price_out_of_range"),
            (5, [bar("04:17", 11, 12, 8, 9), bar("09:20", 9)], None, "entry_price_out_of_range"),
            (5, [bar("04:17", 11), bar("04:18", 9), bar("09:20", 9)], 9, ""),
        )
        for percent, following, expected_fill, expected_reason in cases:
            with self.subTest(percent=percent, fill=expected_fill):
                config = replace(self.config, reentry=ReentryConfig(
                    enabled=True, entry_above_high_percent=percent,
                ))
                history = stopped + following
                initial, retry = simulate_trades(DAY, "TEST", history, PREVIOUS, config)
                self.assertEqual(initial.exit_reason, "stop_loss")
                self.assertEqual(retry.entry_price, expected_fill)
                self.assertEqual(retry.reason, expected_reason)
                self.assertEqual(simulate_prepared(prepare_case(initial, history), config), retry)


if __name__ == "__main__":
    unittest.main()
