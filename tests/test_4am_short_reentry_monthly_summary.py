"""Re-entry reports isolate trade two while retaining every processed month."""

import unittest

from four_am_short.models import TradeResult
from four_am_short.reports import format_monthly_summary, reentry_monthly_summaries


def trade(day, symbol, pnl, *, number=2, clock="05:00", reason="time_exit"):
    return TradeResult(
        day, symbol, "trade", trade_number=number,
        exit_time=f"{day}T{clock}:00-05:00", exit_reason=reason,
        gross_pnl=pnl + 2, commission=2, locate_cost=0, net_pnl=pnl,
    )


def table_row(text, period):
    for line in text.splitlines():
        cells = line.split()
        if cells and cells[0] == period:
            return cells
    raise AssertionError(f"No {period!r} row in summary:\n{text}")


def money(value):
    return float(value.replace("$", "").replace(",", ""))


class ReentryMonthlySummaryTests(unittest.TestCase):
    def test_months_use_only_reentry_net_outcomes_days_and_stop_exits(self):
        rows = [
            trade("2026-01-05", "AAA", -500, number=1, clock="04:30", reason="stop_loss"),
            trade("2026-01-05", "AAA", 100, reason="profit_target"),
            trade("2026-01-06", "AAA", 900, number=1, clock="04:30", reason="profit_target"),
            trade("2026-01-06", "AAA", -40, reason="stop_loss"),
            trade("2026-01-07", "AAA", -500, number=1, clock="04:30", reason="stop_loss"),
            trade("2026-01-07", "AAA", 0),
        ]
        stats = reentry_monthly_summaries(rows)[0]
        self.assertEqual(stats["period"], "2026-01")
        self.assertEqual(stats["initial_trades"], 0)
        self.assertEqual(stats["trades"], 3)
        self.assertEqual(stats["reentry_trades"], 3)
        self.assertEqual((stats["wins"], stats["losses"], stats["breakeven"]), (1, 1, 1))
        self.assertEqual((stats["winning_days"], stats["losing_days"], stats["breakeven_days"]), (1, 1, 1))
        self.assertEqual((stats["net_pnl"], stats["average_net_pnl"], stats["profit_factor"]), (60, 20, 2.5))
        self.assertEqual(stats["stops"], 1)
        self.assertEqual(stats["targets"], 1)
        self.assertEqual(stats["max_closed_trade_drawdown"], 40)
        text = format_monthly_summary(rows, reentry_only=True)
        self.assertIn("Re-entry monthly summary - 4am short", text)
        result = table_row(text, "2026-01")
        self.assertEqual(result[1:6], ["3", "1", "1", "33.33%", "33.33%"])
        self.assertEqual([money(result[i]) for i in (6, 7, 9)], [60, 20, 40])
        self.assertEqual(result[8], "2.50")
        self.assertEqual(result[10:], ["1", "1", "1"])
        # The original combined table remains combined, including initial stops.
        combined = table_row(format_monthly_summary(rows), "TOTAL")
        self.assertEqual(combined[1], "6")
        self.assertEqual(combined[-1], "3")
        self.assertEqual(money(combined[6]), -40)

    def test_totals_pool_reentries_and_follow_equity_across_month_boundaries(self):
        rows = [
            trade("2026-01-29", "AAA", 100, clock="04:30"),
            trade("2026-01-29", "BBB", -60, clock="04:31", reason="stop_loss"),
            trade("2026-02-02", "AAA", -50, reason="stop_loss"),
            trade("2026-02-03", "AAA", 80),
            trade("2026-02-04", "AAA", -70, reason="stop_loss"),
            trade("2026-03-02", "INITIAL", 10000, number=1),
            TradeResult("2026-04-01", "SKIP", "skipped", "no_early_bars"),
        ]
        months = reentry_monthly_summaries(list(reversed(rows)))
        self.assertEqual([row["period"] for row in months], ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual([row["trades"] for row in months], [2, 3, 0, 0])
        self.assertEqual([row["max_closed_trade_drawdown"] for row in months], [60, 70, 0, 0])
        text = format_monthly_summary(rows, reentry_only=True)
        total = table_row(text, "TOTAL")
        self.assertEqual(total[1:6], ["5", "2", "3", "40.00%", "60.00%"])
        self.assertEqual([money(total[i]) for i in (6, 7, 9)], [0, 0, 110])
        self.assertEqual(total[8], "1.00")
        self.assertEqual(total[-1], "3")
        for period in ("2026-03", "2026-04"):
            zero = table_row(text, period)
            self.assertEqual(zero[1:6], ["0", "0", "0", "0.00%", "0.00%"])
            self.assertEqual(zero[7:9], ["--", "--"])

    def test_unfinished_and_skipped_reentries_remain_counted_but_not_performance(self):
        rows = [
            trade("2026-01-05", "AAA", -2, reason="stop_loss"),
            TradeResult("2026-01-06", "BBB", "skipped", "reentry_not_filled", trade_number=2, net_pnl=100000),
            TradeResult("2026-01-07", "CCC", "incomplete", "missing_time_exit_bar", trade_number=2,
                        net_pnl=100000, exit_reason="stop_loss"),
            TradeResult("2026-01-08", "DDD", "error", "data_error", trade_number=2,
                        net_pnl=100000, exit_reason="stop_loss"),
            TradeResult("2026-01-09", "FIRST", "error", "data_error"),
        ]
        stats = reentry_monthly_summaries(rows)[0]
        self.assertEqual((stats["trades"], stats["skipped"], stats["incomplete"], stats["errors"]), (1, 1, 1, 1))
        self.assertEqual(stats["reentry_attempts"], 4)
        self.assertEqual((stats["net_pnl"], stats["stops"], stats["profit_factor"]), (-2, 1, 0))
        text = format_monthly_summary(rows, reentry_only=True, partial=True)
        self.assertIn("Re-entry monthly summary - 4am short (PARTIAL)", text)
        self.assertIn("1 attempt errors", text)
        self.assertIn("1 incomplete", text)
        self.assertEqual(table_row(text, "TOTAL")[1:4], ["1", "0", "1"])
        self.assertEqual(table_row(text, "TOTAL")[8], "0.00")
        for heading in ("Month", "Trades", "Wins", "Losses", "Win%", "Loss%", "Net P/L",
                        "Avg/Trade", "PF", "Max DD", "Win Days", "Loss Days", "Stop Stocks"):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)

    def test_empty_and_initial_only_runs_have_no_invented_reentry_results(self):
        self.assertEqual(reentry_monthly_summaries([]), [])
        for rows in ([], [trade("2026-01-05", "AAA", 10000, number=1)]):
            with self.subTest(empty=not rows):
                total = table_row(format_monthly_summary(rows, reentry_only=True), "TOTAL")
                self.assertEqual(total[1:6], ["0", "0", "0", "0.00%", "0.00%"])
                self.assertEqual(total[7:9], ["--", "--"])
                self.assertEqual(total[10:], ["0", "0", "0"])


if __name__ == "__main__":
    unittest.main()
