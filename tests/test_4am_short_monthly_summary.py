"""Monthly performance must aggregate actual trades, dates, and equity paths."""

import re
import unittest

from four_am_short.models import TradeResult
from four_am_short.reports import format_monthly_summary, monthly_summaries, summarize


def trade(day, symbol, pnl, *, reason="time_exit", clock="05:00"):
    return TradeResult(
        day, symbol, "trade", exit_time=f"{day}T{clock}:00-05:00",
        exit_reason=reason, gross_pnl=pnl + 2, commission=2,
        locate_cost=0, net_pnl=pnl,
    )


def table_row(text, label):
    for line in text.splitlines():
        cells = line.replace("|", " ").split()
        if cells and cells[0] == label:
            return cells
    raise AssertionError(f"No {label!r} row in monthly table:\n{text}")


def money(token):
    """Allow currency-sign presentation choices while requiring cents."""
    if not re.fullmatch(r"[+-]?\$?\d[\d,]*\.\d{2}", token):
        raise AssertionError(f"Expected a monetary amount with cents, got {token!r}")
    return float(token.replace("$", "").replace(",", ""))


class MonthlySummaryTests(unittest.TestCase):
    def test_day_outcomes_use_net_daily_pnl_and_stops_total_every_stopped_trade(self):
        rows = [
            trade("2026-01-05", "AAA", -60, reason="stop_loss"),
            trade("2026-01-05", "BBB", -40, reason="stop_loss"),
            trade("2026-01-05", "CCC", 150, reason="profit_target"),
            trade("2026-01-06", "AAA", -80, reason="stop_loss"),
            trade("2026-01-06", "BBB", -30),
            trade("2026-01-06", "CCC", 10, reason="profit_target"),
            trade("2026-01-07", "AAA", -20, reason="stop_loss"),
            trade("2026-01-07", "BBB", 20, reason="profit_target"),
            TradeResult("2026-01-08", "AAA", "skipped", "no_early_bars"),
            TradeResult("2026-01-09", "AAA", "incomplete", "missing_time_exit_bar", net_pnl=10000),
            TradeResult("2026-01-12", "AAA", "error", "market_data_error", exit_reason="stop_loss"),
        ]
        result = summarize(rows)
        self.assertEqual(result["trades"], 8)
        self.assertEqual(result["wins"], 3)
        self.assertEqual(result["losses"], 5)
        self.assertEqual(result["win_percent"], 37.5)
        self.assertEqual(result["loss_percent"], 62.5)
        self.assertEqual(result["net_pnl"], -50)
        self.assertEqual(result["winning_days"], 1)
        self.assertEqual(result["losing_days"], 1)
        self.assertEqual(result["breakeven_days"], 1)
        self.assertEqual(result["stops"], 4)
        self.assertNotIn("max_stop_stocks", result)
        # AAA stops on three dates: each trade counts, rather than taking the
        # largest daily count (two) or the number of distinct tickers (two).
        rows.extend([
            trade("2026-02-02", "AAA", -50, reason="stop_loss"),
            trade("2026-02-03", "AAA", -30, reason="stop_loss"),
        ])
        self.assertEqual([month["stops"] for month in monthly_summaries(rows)], [4, 2])
        text = format_monthly_summary(rows)
        self.assertEqual(table_row(text, "2026-01")[-1], "4")
        self.assertEqual(table_row(text, "2026-02")[-1], "2")
        self.assertEqual(table_row(text, "TOTAL")[-1], "6")

    def test_month_drawdown_resets_but_total_follows_full_equity_path(self):
        rows = [
            trade("2026-01-29", "AAA", 100, clock="04:30"),
            trade("2026-01-29", "BBB", -60, clock="04:31"),
            trade("2026-02-02", "AAA", -50),
            trade("2026-02-03", "AAA", 80),
            trade("2026-02-04", "AAA", -70),
        ]
        months = monthly_summaries(list(reversed(rows)))
        self.assertEqual([row["period"] for row in months], ["2026-01", "2026-02"])
        self.assertEqual([row["max_closed_trade_drawdown"] for row in months], [60, 70])
        self.assertEqual(summarize(rows)["max_closed_trade_drawdown"], 110)
        total = table_row(format_monthly_summary(rows), "TOTAL")
        self.assertEqual(money(total[9]), 110)

    def test_weighted_total_pf_and_average_with_zero_trade_month(self):
        rows = [
            trade("2026-01-05", "AAA", 100),
            trade("2026-01-06", "BBB", -50),
            trade("2026-01-07", "FLAT", 0),
            trade("2026-02-02", "AAA", 900),
            TradeResult("2026-03-02", "AAA", "skipped", "no_early_bars"),
            TradeResult("2026-03-03", "BBB", "error", "market_data_error"),
        ]
        months = monthly_summaries(rows)
        self.assertEqual([row["period"] for row in months], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual([row["trades"] for row in months], [3, 1, 0])
        self.assertEqual([row["wins"] for row in months], [1, 1, 0])
        self.assertEqual([row["losses"] for row in months], [1, 0, 0])
        self.assertEqual(months[0]["win_percent"], 33.3333)
        self.assertEqual(months[0]["loss_percent"], 33.3333)
        self.assertNotIn("TOTAL", [row["period"] for row in months])
        self.assertEqual(months[-1]["breakeven_days"], 0)
        text = format_monthly_summary(rows)
        jan, feb, march, total = [table_row(text, label) for label in ("2026-01", "2026-02", "2026-03", "TOTAL")]
        self.assertEqual(jan[2:4], ["1", "1"])
        self.assertEqual(jan[4:6], ["33.33%", "33.33%"])
        self.assertEqual(jan[8], "2.00")
        self.assertEqual(feb[2:6], ["1", "0", "100.00%", "0.00%"])
        self.assertEqual(feb[8], "inf")
        self.assertEqual(march[2:6], ["0", "0", "0.00%", "0.00%"])
        self.assertEqual(march[7:9], ["--", "--"])
        self.assertEqual(total[1:6], ["4", "2", "1", "50.00%", "25.00%"])
        self.assertEqual(money(total[6]), 950)
        self.assertEqual(money(total[7]), 237.5)
        self.assertEqual(total[8], "20.00")

    def test_empty_and_breakeven_results_do_not_invent_infinite_pf(self):
        self.assertEqual(monthly_summaries([]), [])
        empty = table_row(format_monthly_summary([]), "TOTAL")
        self.assertEqual(empty[1:6], ["0", "0", "0", "0.00%", "0.00%"])
        self.assertEqual(empty[7:9], ["--", "--"])
        rows = [trade("2026-01-05", "AAA", 0)]
        summary = summarize(rows)
        self.assertEqual(summary["breakeven_days"], 1)
        self.assertEqual(summary["winning_days"], 0)
        self.assertEqual(summary["losing_days"], 0)
        self.assertEqual(summary["wins"], 0)
        self.assertEqual(summary["losses"], 0)
        self.assertEqual(summary["win_percent"], 0)
        self.assertEqual(summary["loss_percent"], 0)
        flat = table_row(format_monthly_summary(rows), "TOTAL")
        self.assertEqual(flat[1:6], ["1", "0", "0", "0.00%", "0.00%"])
        self.assertEqual(money(flat[7]), 0)
        self.assertEqual(flat[8], "--")

    def test_partial_heading_columns_and_excluded_candidate_warning(self):
        rows = [
            TradeResult("2026-01-05", "AAA", "error", "market_data_error"),
            TradeResult("2026-01-06", "BBB", "incomplete", "missing_time_exit_bar"),
        ]
        text = format_monthly_summary(rows, partial=True)
        self.assertIn("Monthly summary - 4am short", text)
        self.assertIn("(PARTIAL)", text)
        for heading in ("Month", "Trades", "Wins", "Losses", "Win%", "Loss%", "Net P/L", "Avg/Trade", "PF", "Max DD", "Win Days", "Loss Days", "Stop Stocks"):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)
        self.assertNotIn("Max Stop Stocks", text)
        self.assertRegex(text.lower(), r"errors?")
        self.assertIn("incomplete", text.lower())
        self.assertEqual(table_row(text, "2026-01")[1], "0")
        self.assertEqual(table_row(text, "TOTAL")[1], "0")


if __name__ == "__main__":
    unittest.main()
