"""Human-readable output must distinguish prices, wait clocks, and outcomes."""

from dataclasses import replace
import unittest

from four_am_short.config import StrategyConfig
from four_am_short.models import TradeResult
from four_am_short.trade_details import format_candidate


def completed_trade(**changes):
    result = TradeResult(
        date="2026-01-05", symbol="TEST", status="trade",
        previous_close_date="2026-01-02", previous_close=10, gap_threshold=13,
        first_gap_time="2026-01-05T04:00:00-05:00", first_gap_bar_high=13.12345678,
        early_high=20, early_high_bar_time="2026-01-05T04:03:00-05:00",
        early_high_time="2026-01-05T04:04:00-05:00", high_time_basis="bar_end",
        order_active_time="2026-01-05T04:15:00-05:00", entry_limit=18,
        entry_time="2026-01-05T04:15:00-05:00", entry_price=18, shares=1000,
        stop_price=23.4, target_price=15.75,
        exit_time="2026-01-05T04:20:00-05:00", exit_price=15.75,
        exit_reason="profit_target", profit_target_hit=True,
        gross_pnl=2250, commission=10, locate_cost=100, net_pnl=2140,
    )
    return replace(result, **changes)


class TradeDetailsTests(unittest.TestCase):
    def test_target_trade_distinguishes_threshold_bar_high_and_wait_clock(self):
        text = format_candidate(completed_trade(), StrategyConfig(), index=2, total=8)
        self.assertIn("[2/8] 2026-01-05 TEST: TRADE | PROFIT TARGET", text)
        self.assertIn("price > $13.00 (+30%)", text)
        self.assertIn("First qualifying bar: 4:00 AM EST; bar high $13.12345678", text)
        self.assertIn("Early high: $20.00; source bar 4:03 AM EST", text)
        self.assertIn("Wait reference: 4:04 AM EST (bar end)", text)
        self.assertIn("SHORT 1,000 shares at $18.00; 4:15 AM EST", text)
        self.assertIn("Stop: $23.40 (+30%); target: $15.75 (-12.5%)", text)
        self.assertIn("COVER at $15.75; 4:20 AM EST; PROFIT TARGET", text)
        self.assertIn("Profit target hit: YES", text)
        self.assertIn("commission: $10.00; locate: $100.00; net P/L: +$2,140.00", text)
        self.assertIn("timestamps label minute-bar starts", text)
        self.assertNotIn("\x1b", text)

    def test_profitable_time_exit_does_not_claim_target_hit(self):
        result = completed_trade(
            exit_time="2026-01-05T09:30:00-05:00", exit_price=17,
            exit_reason="time_exit", profit_target_hit=False, gross_pnl=1000, net_pnl=890,
        )
        text = format_candidate(result, StrategyConfig())
        self.assertIn("TIME EXIT | Net +$890.00", text)
        self.assertIn("COVER at $17.00; 9:30 AM EST; TIME EXIT", text)
        self.assertIn("Profit target hit: NO", text)
        self.assertNotIn("Profit target hit: YES", text)

    def test_open_incomplete_position_has_unresolved_target_and_no_realized_pnl(self):
        result = completed_trade(
            status="incomplete", reason="missing_time_exit_bar",
            exit_time="", exit_price=None, exit_reason="", profit_target_hit=None,
            gross_pnl=None, commission=None, locate_cost=None, net_pnl=None,
        )
        text = format_candidate(result, StrategyConfig())
        self.assertIn("Entry: SHORT 1,000 shares", text)
        self.assertIn("Exit: UNRESOLVED", text)
        self.assertIn("Profit target hit: UNRESOLVED", text)
        self.assertIn("Realized P/L: UNAVAILABLE", text)
        self.assertNotIn("Net $0.00", text)

    def test_qualifying_unfilled_order_keeps_signal_and_order_details(self):
        result = completed_trade(
            status="skipped", reason="entry_not_filled_before_deadline",
            entry_price=None, entry_time="", shares=0, stop_price=None, target_price=None,
            exit_time="", exit_price=None, exit_reason="", profit_target_hit=None,
            gross_pnl=None, commission=None, locate_cost=None, net_pnl=None,
        )
        text = format_candidate(result, StrategyConfig())
        self.assertIn("First qualifying bar: 4:00 AM EST", text)
        self.assertIn("Sell limit: $18.00 (10% below early high)", text)
        self.assertIn("Order active: 4:15 AM EST; entry strictly before 6:00 AM EST", text)
        self.assertIn("Entry: NOT FILLED", text)
        self.assertIn("profit target hit: N/A", text)
        self.assertNotIn("COVER", text)

    def test_no_early_data_and_api_errors_remain_compact(self):
        skipped = TradeResult("2026-01-05", "TEST", "skipped", "no_early_bars")
        error = TradeResult("2026-01-05", "TEST", "error", "market_data_error", notes="Massive HTTP 403")
        for result in (skipped, error):
            with self.subTest(status=result.status):
                text = format_candidate(result, StrategyConfig())
                self.assertEqual(len(text.splitlines()), 1)
                self.assertNotIn("Profit target hit: YES", text)
        self.assertIn("Massive HTTP 403", format_candidate(error, StrategyConfig()))

    def test_utc_event_times_display_in_eastern_daylight_time(self):
        result = completed_trade(
            date="2026-06-01", first_gap_time="2026-06-01T08:00:00+00:00",
            early_high_bar_time="2026-06-01T08:03:00+00:00",
            early_high_time="2026-06-01T08:04:00+00:00",
            order_active_time="2026-06-01T08:15:00+00:00",
            entry_time="2026-06-01T08:15:00+00:00",
            exit_time="2026-06-01T08:20:00+00:00",
        )
        text = format_candidate(result, StrategyConfig())
        self.assertIn("First qualifying bar: 4:00 AM EDT", text)
        self.assertIn("entry strictly before 6:00 AM EDT", text)
        self.assertNotIn("UTC", text)

    def test_late_setup_describes_its_own_fixed_window_and_high(self):
        result = completed_trade(
            first_gap_time="2026-01-05T07:00:00-05:00",
            early_high_bar_time="2026-01-05T07:14:00-05:00",
            early_high_time="2026-01-05T07:15:00-05:00",
            order_active_time="2026-01-05T07:25:00-05:00",
            entry_time="2026-01-05T07:25:00-05:00",
            exit_time="2026-01-05T07:30:00-05:00",
        )
        config = StrategyConfig(late_gap_enabled=True, entry_deadline="09:00")
        text = format_candidate(result, config)
        self.assertIn("Discovery window: 4:00 AM EST to 9:00 AM EST (end excluded)", text)
        self.assertIn("Late setup window: 7:00 AM EST to 7:15 AM EST (end excluded)", text)
        self.assertIn("Late setup high: $20.00; source bar 7:14 AM EST", text)
        self.assertIn("Wait reference: 7:15 AM EST (bar end); wait 10 minutes", text)
        self.assertIn("Sell limit: $18.00 (10% below late setup high)", text)
        self.assertIn("Order active: 7:25 AM EST; entry strictly before 9:00 AM EST", text)
        self.assertNotIn("Early window:", text)
        self.assertNotIn("Early high:", text)

    def test_late_setup_uses_configured_window_and_eastern_first_gap_boundary(self):
        config = StrategyConfig(late_gap_enabled=True, late_gap_window_minutes=20, entry_deadline="09:00")
        result = completed_trade(date="2026-06-01", first_gap_time="2026-06-01T08:15:00+00:00")
        text = format_candidate(result, config)
        self.assertIn("Late setup window: 4:15 AM EDT to 4:35 AM EDT (end excluded)", text)
        self.assertIn("Late setup high:", text)
        early = replace(result, first_gap_time="2026-06-01T08:14:00+00:00")
        early_text = format_candidate(early, config)
        self.assertIn("Early window: 4:00 AM EDT to 4:15 AM EDT", early_text)
        self.assertIn("Early high:", early_text)
        self.assertNotIn("Late setup high:", early_text)

    def test_reentry_keeps_late_setup_high_and_its_separate_deadline(self):
        config = StrategyConfig(late_gap_enabled=True, entry_deadline="09:00")
        config = replace(config, reentry=replace(config.reentry, enabled=True, entry_deadline="08:00"))
        result = completed_trade(trade_number=2, first_gap_time="2026-01-05T07:00:00-05:00")
        text = format_candidate(result, config)
        self.assertIn("original late setup high is retained", text)
        self.assertIn("5% above late setup high", text)
        self.assertIn("entry strictly before 8:00 AM EST", text)


if __name__ == "__main__":
    unittest.main()
