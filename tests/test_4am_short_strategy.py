"""Boundary and execution tests using tiny, fully inspectable price histories."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest
from zoneinfo import ZoneInfo

from four_am_short.config import StrategyConfig
from four_am_short.models import Bar, DataError, PreviousClose
from four_am_short.strategy import simulate


ET = ZoneInfo("America/New_York")
DAY = date(2026, 1, 5)
PREVIOUS = PreviousClose(date(2026, 1, 2), 10)


def bar(clock, opening, high=None, low=None, close=None, *, day=DAY):
    return Bar(
        datetime.fromisoformat(f"{day.isoformat()}T{clock}:00").replace(tzinfo=ET),
        opening,
        opening if high is None else high,
        opening if low is None else low,
        opening if close is None else close,
        1000,
    )


class StrategyTests(unittest.TestCase):
    def run_case(self, following=(), *, early=None, config=None, previous=PREVIOUS):
        history = [bar("04:00", 18, 20, 17, 18)] if early is None else list(early)
        return simulate(DAY, "TEST", history + list(following), previous, config or StrategyConfig())

    def test_gap_requires_strictly_more_than_30_percent(self):
        exact = self.run_case(early=[bar("04:00", 13)])
        self.assertEqual(exact.status, "skipped")
        self.assertEqual(exact.reason, "gap_threshold_not_exceeded")
        above = self.run_case(early=[bar("04:00", 13.0001)])
        self.assertTrue(above.first_gap_time)
        self.assertEqual(above.gap_threshold, 13)

    def test_first_and_last_minutes_of_early_window(self):
        result = self.run_case(
            early=[bar("03:59", 30), bar("04:00", 13.01), bar("04:14", 14), bar("04:15", 99)]
        )
        self.assertEqual(result.early_high, 14)
        self.assertEqual(result.first_gap_time, "2026-01-05T04:00:00-05:00")
        self.assertEqual(result.first_gap_bar_high, 13.01)
        self.assertEqual(result.early_high_bar_time, "2026-01-05T04:14:00-05:00")
        self.assertEqual(result.early_high_time, "2026-01-05T04:15:00-05:00")
        self.assertEqual(result.order_active_time, "2026-01-05T04:25:00-05:00")

    def test_gap_after_early_window_does_not_qualify(self):
        result = self.run_case(early=[bar("04:14", 12.9), bar("04:15", 20)])
        self.assertEqual(result.reason, "gap_threshold_not_exceeded")

    def test_window_must_finish_even_when_wait_has_elapsed(self):
        result = self.run_case([bar("04:11", 18), bar("04:15", 18), bar("09:30", 17)])
        self.assertEqual(result.entry_time, "2026-01-05T04:15:00-05:00")

    def test_ten_minutes_after_end_of_latest_high_bar(self):
        result = self.run_case(
            [bar("04:15", 19), bar("04:24", 19), bar("04:25", 18), bar("09:30", 17)],
            early=[bar("04:00", 14), bar("04:14", 19, 20, 18, 19)],
        )
        self.assertEqual(result.entry_time, "2026-01-05T04:25:00-05:00")
        self.assertEqual(result.entry_price, 18)

    def test_repeated_high_defaults_to_last_but_first_is_available(self):
        history = [bar("04:02", 20), bar("04:10", 20)]
        following = [bar("04:15", 19), bar("04:21", 18), bar("09:30", 17)]
        last = self.run_case(following, early=history)
        first = self.run_case(following, early=history, config=StrategyConfig(repeated_high_policy="first"))
        self.assertEqual(last.entry_time, "2026-01-05T04:21:00-05:00")
        self.assertEqual(first.entry_time, "2026-01-05T04:15:00-05:00")

    def test_bar_start_high_reference_is_explicit_opt_in(self):
        result = self.run_case(
            [bar("04:20", 18), bar("09:30", 17)],
            early=[bar("04:10", 20)],
            config=StrategyConfig(high_time_reference="bar_start"),
        )
        self.assertEqual(result.order_active_time, "2026-01-05T04:20:00-05:00")

    def test_sell_limit_improves_at_open_and_sets_actual_entry_exits(self):
        result = self.run_case([bar("04:15", 19), bar("09:30", 18)])
        self.assertEqual(result.entry_limit, 18)
        self.assertEqual(result.entry_price, 19)
        self.assertEqual(result.stop_price, 24.7)
        self.assertEqual(result.target_price, 16.625)
        self.assertEqual(result.net_pnl, 1000)

    def test_waits_for_bounce_then_fills_at_limit(self):
        result = self.run_case(
            [bar("04:15", 17, 17.99, 16, 17), bar("04:16", 17, 18, 16, 17), bar("09:30", 17)]
        )
        self.assertEqual(result.entry_time, "2026-01-05T04:16:00-05:00")
        self.assertEqual(result.entry_price, 18)

    def test_no_fill_at_exact_deadline(self):
        result = self.run_case([bar("05:59", 17), bar("06:00", 19), bar("09:30", 17)])
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "entry_not_filled_before_deadline")
        self.assertIsNone(result.entry_price)

    def test_last_minute_before_deadline_can_fill(self):
        result = self.run_case([bar("05:59", 18), bar("09:30", 17)])
        self.assertEqual(result.status, "trade")
        self.assertEqual(result.entry_time, "2026-01-05T05:59:00-05:00")

    def test_activation_at_deadline_skips(self):
        result = self.run_case(config=StrategyConfig(wait_after_high_minutes=119))
        self.assertEqual(result.reason, "activation_at_or_after_deadline")

    def test_stop_wins_when_open_position_hits_both_exits(self):
        following = [bar("04:15", 18), bar("04:16", 18, 24, 15, 18)]
        conservative = self.run_case(following)
        low_first = self.run_case(following, config=StrategyConfig(intrabar_policy="olhc"))
        self.assertEqual(conservative.exit_reason, "stop_loss")
        self.assertFalse(conservative.profit_target_hit)
        self.assertAlmostEqual(conservative.exit_price, 23.4)
        self.assertEqual(conservative.ambiguous_bars, 1)
        self.assertEqual(low_first.exit_reason, "profit_target")
        self.assertTrue(low_first.profit_target_hit)

    def test_low_before_bounce_is_not_a_fabricated_profit(self):
        following = [bar("04:15", 16, 18.1, 15, 17), bar("09:30", 17)]
        conservative = self.run_case(following)
        high_first = self.run_case(following, config=StrategyConfig(intrabar_policy="ohlc"))
        low_first = self.run_case(following, config=StrategyConfig(intrabar_policy="olhc"))
        self.assertEqual(conservative.exit_reason, "time_exit")
        self.assertFalse(conservative.profit_target_hit)
        self.assertEqual(conservative.net_pnl, 1000)
        self.assertEqual(conservative.ambiguous_bars, 1)
        self.assertEqual(high_first.exit_reason, "profit_target")
        self.assertEqual(low_first.exit_reason, "time_exit")

    def test_same_bar_target_allowed_if_both_paths_reach_it_after_entry(self):
        result = self.run_case([bar("04:15", 16, 18.1, 15, 15.5)])
        self.assertEqual(result.exit_reason, "profit_target")
        self.assertEqual(result.exit_price, 15.75)
        self.assertEqual(result.ambiguous_bars, 0)

    def test_bounce_can_stop_in_its_entry_bar(self):
        result = self.run_case([bar("04:15", 16, 24, 15, 17)])
        self.assertEqual(result.entry_price, 18)
        self.assertEqual(result.exit_reason, "stop_loss")
        self.assertEqual(result.entry_time, result.exit_time)

    def test_gapped_stop_fills_at_open(self):
        result = self.run_case([bar("04:15", 18), bar("04:16", 25, 26, 24, 25)])
        self.assertEqual(result.exit_reason, "stop_loss")
        self.assertEqual(result.exit_price, 25)
        self.assertEqual(result.gross_pnl, -7000)

    def test_target_gap_does_not_assume_price_improvement(self):
        result = self.run_case([bar("04:15", 18), bar("04:16", 14, 15, 13, 14)])
        self.assertEqual(result.exit_reason, "profit_target")
        self.assertEqual(result.exit_price, 15.75)

    def test_exit_at_cutoff_open_ignores_later_range(self):
        result = self.run_case([bar("04:15", 18), bar("09:30", 17, 30, 10, 18)])
        self.assertEqual(result.exit_reason, "time_exit")
        self.assertEqual(result.exit_price, 17)

    def test_missing_exact_cutoff_is_incomplete_without_realized_pnl(self):
        result = self.run_case([bar("04:15", 18), bar("09:29", 17), bar("09:31", 16)])
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.reason, "missing_time_exit_bar")
        self.assertIsNone(result.exit_price)
        self.assertIsNone(result.net_pnl)
        self.assertIsNone(result.profit_target_hit)

    def test_round_trip_costs_and_adverse_slippage(self):
        config = StrategyConfig(
            entry_slippage_bps=100,
            exit_slippage_bps=100,
            commission_per_share_per_side=0.005,
            locate_fee_per_share=0.1,
        )
        result = self.run_case([bar("04:15", 20), bar("09:30", 19)], config=config)
        self.assertAlmostEqual(result.entry_price, 19.8)
        self.assertAlmostEqual(result.exit_price, 19.19)
        self.assertAlmostEqual(result.gross_pnl, 610)
        self.assertEqual(result.commission, 10)
        self.assertEqual(result.locate_cost, 100)
        self.assertAlmostEqual(result.net_pnl, 500)

    def test_adverse_entry_slippage_cannot_violate_sell_limit(self):
        config = StrategyConfig(entry_slippage_bps=1000)
        for opening in (17, 19):
            with self.subTest(opening=opening):
                result = self.run_case([bar("04:15", opening, 19, 17, 18), bar("09:30", 17)], config=config)
                self.assertEqual(result.entry_price, 18)

    def test_position_size_is_configurable(self):
        result = self.run_case([bar("04:15", 18), bar("09:30", 17)], config=StrategyConfig(shares=200))
        self.assertEqual(result.shares, 200)
        self.assertEqual(result.gross_pnl, 200)

    def test_exit_ends_day_without_reentry(self):
        result = self.run_case([bar("04:15", 18), bar("04:16", 15), bar("04:17", 19), bar("09:30", 18)])
        self.assertEqual(result.exit_time, "2026-01-05T04:16:00-05:00")
        self.assertEqual(result.exit_reason, "profit_target")

    def test_eastern_times_follow_dst_and_convert_utc_input(self):
        for day, offset in ((date(2026, 1, 5), "-05:00"), (date(2026, 6, 1), "-04:00")):
            with self.subTest(day=day):
                history = [bar("04:00", 20, day=day), bar("04:15", 18, day=day), bar("09:30", 17, day=day)]
                utc = [replace(b, timestamp=b.timestamp.astimezone(timezone.utc)) for b in history]
                result = simulate(day, "TEST", utc, PreviousClose(day - timedelta(days=3), 10), StrategyConfig())
                self.assertEqual(result.entry_time, f"{day.isoformat()}T04:15:00{offset}")
                self.assertEqual(result.exit_time, f"{day.isoformat()}T09:30:00{offset}")

    def test_split_normalization_audit_is_preserved(self):
        result = self.run_case(previous=PreviousClose(date(2026, 1, 2), 10, 1, 10))
        self.assertEqual(result.previous_close, 10)
        self.assertIn("split-normalized", result.notes)
        self.assertIn("factor=10", result.notes)

    def test_input_order_does_not_change_result(self):
        history = [bar("09:30", 17), bar("04:15", 18), bar("04:00", 20)]
        result = simulate(DAY, "TEST", history, PREVIOUS, StrategyConfig())
        self.assertEqual(result.net_pnl, 1000)

    def test_invalid_duplicate_or_naive_bars_fail_explicitly(self):
        valid = bar("04:00", 20)
        for history in (
            [valid, valid],
            [replace(valid, timestamp=valid.timestamp.replace(tzinfo=None))],
            [replace(valid, timestamp=valid.timestamp.replace(second=30))],
            [replace(valid, high=19)],
            [replace(valid, close=float("nan"))],
        ):
            with self.subTest(history=history):
                with self.assertRaises(DataError):
                    simulate(DAY, "TEST", history, PREVIOUS, StrategyConfig())


if __name__ == "__main__":
    unittest.main()
