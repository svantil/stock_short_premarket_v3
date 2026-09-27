"""Late movers get their own fixed observation window before the same trade."""

from dataclasses import replace
from datetime import date, datetime, timezone
import unittest

from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.models import Bar, EASTERN, PreviousClose
from four_am_short.strategy import simulate, simulate_trades


DAY = date(2026, 1, 5)
PREVIOUS = PreviousClose(date(2026, 1, 2), 10)


def bar(clock, price, *, high=None, low=None, close=None):
    return Bar(datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
               price, price if high is None else high, price if low is None else low,
               price if close is None else close)


class LateGapTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(late_gap_enabled=True, entry_deadline="09:00")

    def run_case(self, history, **changes):
        return simulate(DAY, "LATE", history, PREVIOUS, replace(self.config, **changes))

    def test_later_first_qualifier_waits_for_complete_fifteen_minute_window(self):
        result = self.run_case([bar("04:00", 12), bar("07:00", 20),
                                bar("07:11", 18), bar("07:14", 18),
                                bar("07:15", 18), bar("09:30", 17)])
        self.assertEqual(result.first_gap_time, "2026-01-05T07:00:00-05:00")
        self.assertEqual(result.order_active_time, "2026-01-05T07:15:00-05:00")
        self.assertEqual(result.entry_time, result.order_active_time)
        self.assertEqual(result.entry_limit, 18)
        self.assertEqual(result.stop_price, 23.4)
        self.assertEqual(result.target_price, 15.75)
        self.assertEqual(result.exit_reason, "time_exit")

    def test_late_high_delays_entry_and_post_window_high_is_ignored(self):
        result = self.run_case([bar("07:00", 14), bar("07:14", 20),
                                bar("07:15", 40), bar("07:24", 18),
                                bar("07:25", 18), bar("09:30", 17)])
        self.assertEqual(result.early_high, 20)
        self.assertEqual(result.early_high_time, "2026-01-05T07:15:00-05:00")
        self.assertEqual(result.entry_time, "2026-01-05T07:25:00-05:00")

    def test_equal_high_policy_and_bar_start_clock_are_preserved(self):
        history = [bar("07:00", 20), bar("07:10", 20), bar("07:15", 18),
                   bar("07:20", 18), bar("07:21", 18), bar("09:30", 17)]
        self.assertEqual(self.run_case(history).entry_time, "2026-01-05T07:21:00-05:00")
        self.assertEqual(self.run_case(history, repeated_high_policy="first").entry_time,
                         "2026-01-05T07:15:00-05:00")
        self.assertEqual(self.run_case(history, high_time_reference="bar_start").entry_time,
                         "2026-01-05T07:20:00-05:00")

    def test_no_early_bars_required_and_disabling_restores_original_discovery(self):
        history = [bar("07:00", 20), bar("07:15", 18), bar("09:30", 17)]
        self.assertEqual(self.run_case(history).status, "trade")
        self.assertEqual(self.run_case(history, late_gap_enabled=False).reason, "no_early_bars")
        self.assertEqual(self.run_case([]).reason, "no_premarket_bars")

    def test_exact_thirty_percent_is_not_a_trigger(self):
        result = self.run_case([bar("06:00", 13), bar("07:00", 13.01),
                                bar("07:15", 12), bar("09:30", 12)])
        self.assertEqual(result.first_gap_time, "2026-01-05T07:00:00-05:00")
        self.assertEqual(self.run_case([bar("07:00", 13)]).reason, "gap_threshold_not_exceeded")

    def test_first_crossing_at_early_window_end_starts_a_late_window(self):
        result = self.run_case([bar("04:14", 13), bar("04:15", 20),
                                bar("04:29", 18), bar("04:30", 18), bar("09:30", 17)])
        self.assertEqual(result.entry_time, "2026-01-05T04:30:00-05:00")

    def test_original_early_setup_is_identical_with_later_discovery_enabled(self):
        history = [bar("04:00", 20), bar("04:15", 18), bar("04:16", 15),
                   bar("07:00", 40), bar("07:15", 36), bar("09:30", 35)]
        self.assertEqual(self.run_case(history), self.run_case(history, late_gap_enabled=False))

    def test_no_qualification_at_or_after_nine(self):
        result = self.run_case([bar("08:59", 13), bar("09:00", 20), bar("09:01", 30)])
        self.assertFalse(result.first_gap_time)

    def test_window_or_high_wait_reaching_deadline_cannot_enter(self):
        for history in ([bar("08:45", 20), bar("09:00", 18)],
                        [bar("08:40", 14), bar("08:49", 20), bar("09:00", 18)],
                        [bar("08:59", 20), bar("09:30", 18)]):
            with self.subTest(history=history):
                result = self.run_case(history)
                self.assertEqual(result.reason, "activation_at_or_after_deadline")
                self.assertIsNone(result.entry_price)

    def test_can_fill_last_minute_but_never_exact_deadline(self):
        start = [bar("08:44", 20), bar("08:58", 17)]
        result = self.run_case(start + [bar("08:59", 18), bar("09:30", 17)])
        self.assertEqual(result.entry_time, "2026-01-05T08:59:00-05:00")
        result = self.run_case(start + [bar("08:59", 17), bar("09:00", 18)])
        self.assertIsNone(result.entry_price)
        self.assertEqual(result.reason, "entry_not_filled_before_deadline")

    def test_fixed_window_does_not_restart_when_price_recrosses_gap(self):
        result = self.run_case([bar("07:00", 20), bar("07:05", 12),
                                bar("07:14", 14), bar("07:15", 18), bar("09:30", 17)])
        self.assertEqual(result.entry_time, "2026-01-05T07:15:00-05:00")
        self.assertEqual(result.early_high, 20)

    def test_unfilled_late_order_keeps_its_original_limit_for_bounce(self):
        result = self.run_case([bar("07:00", 20), bar("07:15", 17),
                                bar("07:50", 17, high=18), bar("09:30", 17)])
        self.assertEqual(result.entry_time, "2026-01-05T07:50:00-05:00")
        self.assertEqual(result.entry_price, 18)

    def test_late_reentry_uses_same_window_high_and_existing_deadline(self):
        config = replace(self.config, reentry=ReentryConfig(enabled=True, entry_deadline="08:00"))
        history = [bar("07:00", 20), bar("07:15", 18), bar("07:16", 24),
                   bar("07:17", 24), bar("09:20", 22)]
        first, retry = simulate_trades(DAY, "LATE", history, PREVIOUS, config)
        self.assertEqual(first.exit_reason, "stop_loss")
        self.assertEqual(retry.early_high, 20)
        self.assertEqual(retry.entry_limit, 21)
        self.assertEqual(retry.entry_price, 24)
        self.assertEqual(retry.exit_reason, "time_exit")
        later = [replace(row, timestamp=row.timestamp.replace(hour=row.timestamp.hour + 1))
                 for row in history[:-1]]
        _, expired = simulate_trades(DAY, "LATE", later, PREVIOUS, config)
        self.assertEqual(expired.reason, "activation_at_or_after_deadline")

    def test_utc_bars_use_eastern_windows(self):
        history = [bar("07:00", 20), bar("07:15", 18), bar("09:30", 17)]
        utc = [replace(row, timestamp=row.timestamp.astimezone(timezone.utc)) for row in history]
        self.assertEqual(self.run_case(utc), self.run_case(history))


if __name__ == "__main__":
    unittest.main()
