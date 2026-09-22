"""Inspect re-entry fills, cutoff boundaries, and reuse of the initial signal."""

from dataclasses import replace
from datetime import date, datetime
import unittest

from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.models import Bar, EASTERN, PreviousClose
from four_am_short.strategy import simulate, simulate_trades
from four_am_short.trade_details import format_candidate


DAY = date(2026, 1, 5)
PREVIOUS = PreviousClose(date(2026, 1, 2), 10)


def bar(clock, opening, high=None, low=None, close=None):
    return Bar(
        datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
        opening, opening if high is None else high,
        opening if low is None else low, opening if close is None else close,
    )


class ReentryTests(unittest.TestCase):
    def setUp(self):
        self.config = StrategyConfig(reentry=ReentryConfig(enabled=True))
        self.start = [bar("04:00", 20), bar("04:15", 18)]
        self.stopped = self.start + [bar("04:16", 18, 25, 17, 24)]

    def run_case(self, following, *, history=None, config=None):
        return simulate_trades(
            DAY, "TEST", (self.stopped if history is None else history) + following,
            PREVIOUS, config or self.config,
        )

    def test_disabled_is_identical_to_existing_single_attempt(self):
        history = self.stopped + [bar("04:17", 24), bar("04:18", 14)]
        config = StrategyConfig()
        self.assertEqual(
            simulate_trades(DAY, "TEST", history, PREVIOUS, config),
            [simulate(DAY, "TEST", history, PREVIOUS, config)],
        )
        self.assertEqual(simulate(DAY, "TEST", history, PREVIOUS, self.config).trade_number, 1)

    def test_only_completed_stop_can_trigger_reentry(self):
        cases = (
            (self.start + [bar("04:16", 15)], "trade", "profit_target"),
            (self.start + [bar("09:30", 20)], "trade", "time_exit"),
            (self.start + [bar("09:29", 18)], "incomplete", ""),
            ([bar("04:00", 20), bar("06:00", 19)], "skipped", ""),
            ([], "skipped", ""),
        )
        for history, status, reason in cases:
            with self.subTest(status=status, reason=reason):
                rows = self.run_case([], history=history)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].status, status)
                self.assertEqual(rows[0].exit_reason, reason)

    def test_original_early_high_and_signal_reused_with_improved_entry(self):
        initial, retry = self.run_case([bar("04:17", 24), bar("04:18", 14)])
        self.assertEqual(initial.exit_reason, "stop_loss")
        self.assertEqual([initial.trade_number, retry.trade_number], [1, 2])
        self.assertEqual(retry.early_high, 20)  # Not the later $25 stop-bar high.
        for field in ("gap_threshold", "first_gap_time", "first_gap_bar_high", "early_high_bar_time", "early_high_time"):
            self.assertEqual(getattr(retry, field), getattr(initial, field))
        self.assertEqual(retry.entry_limit, 21)
        self.assertEqual(retry.entry_price, 24)
        self.assertEqual(retry.stop_price, 28.8)
        self.assertEqual(retry.target_price, 14.4)
        self.assertEqual(retry.exit_price, 14.4)
        self.assertEqual(retry.exit_reason, "profit_target")
        self.assertTrue(retry.profit_target_hit)
        self.assertEqual(retry.shares, initial.shares)

    def test_waits_for_bounce_when_price_has_fallen_below_reentry_limit(self):
        _, retry = self.run_case([
            bar("04:17", 20, 20.99, 19, 20),
            bar("04:18", 20, 21, 19, 20), bar("09:20", 20),
        ])
        self.assertEqual(retry.order_active_time, "2026-01-05T04:17:00-05:00")
        self.assertEqual(retry.entry_time, "2026-01-05T04:18:00-05:00")
        self.assertEqual(retry.entry_price, 21)
        self.assertEqual(retry.exit_reason, "time_exit")

    def test_stop_bar_cannot_supply_reentry_prices(self):
        # The stop minute contains both the retry limit and target. Neither
        # can be reused, because their chronology relative to the stop is unknown.
        history = self.start + [bar("04:16", 18, 25, 10, 24)]
        _, retry = self.run_case([bar("04:17", 20), bar("09:20", 24)], history=history)
        self.assertIsNone(retry.entry_price)
        self.assertEqual(retry.reason, "entry_not_filled_before_deadline")
        self.assertIn("same-bar post-stop prices cannot be inferred", retry.notes)

    def test_no_second_locate_cost_but_both_round_trip_commissions(self):
        config = replace(self.config, shares=100, commission_per_share_per_side=0.005, locate_fee_per_share=0.1)
        initial, retry = self.run_case([bar("04:17", 24), bar("04:18", 14)], config=config)
        self.assertEqual(initial.locate_cost, 10)
        self.assertEqual(retry.locate_cost, 0)
        self.assertEqual(initial.commission, 1)
        self.assertEqual(retry.commission, 1)
        self.assertAlmostEqual(retry.gross_pnl, 960)
        self.assertAlmostEqual(retry.net_pnl, 959)

    def test_reentry_preserves_configured_slippage(self):
        config = replace(self.config, entry_slippage_bps=100, exit_slippage_bps=100)
        _, retry = self.run_case([bar("04:17", 24), bar("09:20", 22)], config=config)
        self.assertAlmostEqual(retry.entry_price, 23.76)
        self.assertAlmostEqual(retry.exit_price, 22.22)
        self.assertAlmostEqual(retry.stop_price, 28.512)
        self.assertAlmostEqual(retry.target_price, 14.256)

    def test_reentry_uses_its_own_deadline_after_initial_deadline(self):
        _, retry = self.run_case([bar("08:00", 20), bar("09:19", 21), bar("09:20", 20)])
        self.assertEqual(retry.entry_time, "2026-01-05T09:19:00-05:00")
        self.assertEqual(retry.exit_time, "2026-01-05T09:20:00-05:00")
        self.assertEqual(retry.exit_reason, "time_exit")

    def test_reentry_cannot_fill_at_exact_deadline(self):
        _, retry = self.run_case([bar("09:19", 20), bar("09:20", 21)])
        self.assertEqual(retry.status, "skipped")
        self.assertEqual(retry.reason, "entry_not_filled_before_deadline")
        self.assertIsNone(retry.entry_price)

    def test_stop_too_late_for_retry_is_recorded_without_entry(self):
        for clock in ("09:19", "09:20", "09:29"):
            with self.subTest(clock=clock):
                initial, retry = self.run_case([], history=self.start + [bar(clock, 24)])
                self.assertEqual(initial.exit_reason, "stop_loss")
                self.assertEqual(retry.status, "skipped")
                self.assertEqual(retry.reason, "activation_at_or_after_deadline")
                self.assertIsNone(retry.entry_price)

    def test_time_exit_uses_exact_cutoff_open_before_intrabar_prices(self):
        _, retry = self.run_case([bar("04:17", 24), bar("09:20", 23, 40, 10, 20)])
        self.assertEqual(retry.exit_reason, "time_exit")
        self.assertEqual(retry.exit_price, 23)
        self.assertFalse(retry.profit_target_hit)

    def test_missing_reentry_cutoff_is_unresolved(self):
        _, retry = self.run_case([bar("04:17", 24), bar("09:19", 23), bar("09:21", 23)])
        self.assertEqual(retry.status, "incomplete")
        self.assertEqual(retry.reason, "missing_time_exit_bar")
        self.assertIsNone(retry.exit_price)
        self.assertIsNone(retry.net_pnl)

    def test_second_stop_does_not_allow_a_third_attempt(self):
        rows = self.run_case([bar("04:17", 24), bar("04:18", 29), bar("04:19", 30), bar("04:20", 10)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].exit_reason, "stop_loss")
        self.assertEqual(rows[1].exit_price, 29)

    def test_custom_reentry_settings_and_separate_deadline(self):
        config = replace(self.config, reentry=ReentryConfig(
            enabled=True, entry_above_high_percent=50,
            stop_loss_percent=10, profit_target_percent=25,
            entry_deadline="08:00", time_exit="08:30",
        ))
        _, retry = self.run_case([bar("07:59", 30), bar("08:30", 29)], config=config)
        self.assertEqual(retry.entry_limit, 30)
        self.assertEqual(retry.stop_price, 33)
        self.assertEqual(retry.target_price, 22.5)
        self.assertEqual(retry.exit_time, "2026-01-05T08:30:00-05:00")
        _, late = self.run_case([bar("08:00", 30), bar("08:30", 29)], config=config)
        self.assertEqual(late.status, "skipped")

    def test_details_identify_both_attempts_and_reentry_rules(self):
        initial, retry = self.run_case([bar("04:17", 24), bar("04:18", 14)])
        first_text = format_candidate(initial, self.config, index=1, total=1)
        retry_text = format_candidate(retry, self.config, index=1, total=1)
        self.assertIn("First entry (trade 1)", first_text)
        self.assertIn("Re-entry (trade 2)", retry_text)
        self.assertTrue(retry_text.startswith("[1/1]"))
        self.assertIn("Sell limit: $21.00 (5% above early high)", retry_text)
        self.assertIn("entry strictly before 9:20 AM EST", retry_text)
        self.assertIn("Time exit: 9:20 AM EST", retry_text)
        self.assertIn("Stop: $28.80 (+20%); target: $14.40 (-40%)", retry_text)
        self.assertIn("next minute after the first stop bar", retry_text)
        self.assertIn("no additional locate fee", retry_text)
        self.assertNotIn("wait 10 minutes", retry_text)
        self.assertIn("Profit target hit: YES", retry_text)


if __name__ == "__main__":
    unittest.main()
