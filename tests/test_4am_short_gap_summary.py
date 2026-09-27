"""Gap qualification and entry conversion are separate from closed-trade P/L."""

import unittest

from four_am_short.config import StrategyConfig
from four_am_short.models import TradeResult
from four_am_short.reports import format_gap_summary, monthly_summaries, summarize


def qualified(day, symbol="AAA", *, entered=False, completed=False, reason=""):
    return TradeResult(
        day, symbol, "trade" if completed else "incomplete" if entered else "skipped",
        reason=reason or ("" if completed else "missing_time_exit_bar" if entered else "entry_not_filled_before_deadline"),
        first_gap_time=f"{day}T04:05:00-05:00", first_gap_bar_high=20,
        entry_time=f"{day}T04:16:00-05:00" if entered else "",
        entry_price=18 if entered else None,
        exit_time=f"{day}T09:30:00-05:00" if completed else "",
        exit_reason="time_exit" if completed else "",
        gross_pnl=10 if completed else None,
        commission=0 if completed else None,
        locate_cost=0 if completed else None,
        net_pnl=10 if completed else None,
    )


def table_row(text, label):
    for line in text.splitlines():
        cells = line.replace("|", " ").split()
        if cells and cells[0] == label:
            return cells
    raise AssertionError(f"No {label!r} row in gap summary:\n{text}")


class GapSummaryTests(unittest.TestCase):
    def test_qualifying_unfilled_orders_are_distinct_from_nonqualifying_and_unknown(self):
        rows = [
            qualified("2026-01-05", "NOFILL"),
            qualified("2026-01-05", "TOOLATE", reason="activation_at_or_after_deadline"),
            TradeResult("2026-01-05", "QUIET", "skipped", "no_early_bars"),
            TradeResult("2026-01-05", "EXACT30", "skipped", "gap_threshold_not_exceeded", previous_close=10, early_high=13),
            TradeResult("2026-01-05", "FAILED", "error", "market_data_error"),
            TradeResult("2026-01-05", "FUTURE", "incomplete", "session_not_finished"),
        ]
        summary = summarize(rows)
        self.assertEqual(summary["gap_triggered"], 2)
        self.assertEqual(summary["gap_traded"], 0)
        self.assertEqual(summary["gap_not_traded"], 2)
        self.assertEqual(summary["gap_traded_percent"], 0)
        self.assertEqual(summary["gap_completed"], 0)
        self.assertEqual(summary["gap_unresolved"], 0)
        text = format_gap_summary(rows, StrategyConfig())
        self.assertEqual(table_row(text, "TOTAL")[1:], ["2", "0", "2", "0.00%"])
        readable = text.lower().replace("_", " ")
        self.assertRegex(readable, r"entry not filled|limit did not fill")
        self.assertRegex(readable, r"activation.*(?:deadline|06:00)")
        self.assertRegex(readable, r"unknown|undetermined|not determined|not establish")

    def test_entered_unresolved_position_counts_as_traded_but_not_completed(self):
        rows = [
            qualified("2026-01-05", "DONE", entered=True, completed=True),
            qualified("2026-01-05", "OPEN", entered=True),
        ]
        summary = summarize(rows)
        self.assertEqual(summary["gap_triggered"], 2)
        self.assertEqual(summary["gap_traded"], 2)
        self.assertEqual(summary["gap_not_traded"], 0)
        self.assertEqual(summary["gap_traded_percent"], 100)
        self.assertEqual(summary["gap_completed"], 1)
        self.assertEqual(summary["gap_unresolved"], 1)
        self.assertEqual(summary["trades"], 1)
        self.assertEqual(summary["net_pnl"], 10)
        text = format_gap_summary(rows, StrategyConfig())
        self.assertEqual(table_row(text, "TOTAL")[1:], ["2", "2", "0", "100.00%"])
        self.assertIn("unresolved", text.lower())

    def test_repeated_symbol_on_distinct_days_counts_each_setup_and_total_ratio_is_pooled(self):
        rows = [
            qualified("2026-01-05", entered=True, completed=True),
            qualified("2026-01-06"),
            qualified("2026-01-07"),
            qualified("2026-02-02", entered=True, completed=True),
            qualified("2026-02-03", entered=True, completed=True),
            TradeResult("2026-03-02", "AAA", "skipped", "no_early_bars"),
        ]
        months = monthly_summaries(list(reversed(rows)))
        self.assertEqual([row["period"] for row in months], ["2026-01", "2026-02", "2026-03"])
        self.assertEqual([row["gap_triggered"] for row in months], [3, 2, 0])
        self.assertEqual([row["gap_traded_percent"] for row in months], [33.3333, 100, 0])
        self.assertEqual(summarize(rows)["gap_traded_percent"], 60)
        text = format_gap_summary(rows, StrategyConfig())
        self.assertEqual(table_row(text, "2026-01")[1:], ["3", "1", "2", "33.33%"])
        self.assertEqual(table_row(text, "2026-02")[1:], ["2", "2", "0", "100.00%"])
        self.assertEqual(table_row(text, "2026-03")[1:], ["0", "0", "0", "0.00%"])
        self.assertEqual(table_row(text, "TOTAL")[1:], ["5", "3", "2", "60.00%"])

    def test_empty_partial_summary_describes_configured_qualification(self):
        config = StrategyConfig(gap_percent=42.5, early_start="04:02", early_end="04:12")
        summary = summarize([])
        for name in ("gap_triggered", "gap_traded", "gap_not_traded", "gap_completed", "gap_unresolved", "gap_traded_percent"):
            with self.subTest(name=name):
                self.assertEqual(summary[name], 0)
        text = format_gap_summary([], config, partial=True)
        self.assertIn("(PARTIAL)", text)
        for heading in ("Month", "Gap Triggers", "Traded", "Not Traded", "Traded%"):
            self.assertIn(heading, text)
        self.assertIn("42.5%", text)
        self.assertRegex(text, r"\b0?4:02\b")
        self.assertRegex(text, r"\b0?4:12\b")
        self.assertRegex(text, r"\bET\b|Eastern")
        self.assertEqual(table_row(text, "TOTAL")[1:], ["0", "0", "0", "0.00%"])

    def test_late_gap_discovery_uses_entry_deadline_and_counts_missing_discovery_bars(self):
        config = StrategyConfig(late_gap_enabled=True, entry_deadline="09:00")
        rows = [
            TradeResult("2026-01-05", "LATE", "skipped", "activation_at_or_after_deadline",
                        first_gap_time="2026-01-05T08:50:00-05:00"),
            TradeResult("2026-01-05", "EMPTY", "skipped", "no_premarket_bars"),
        ]
        text = format_gap_summary(rows, config)
        self.assertIn("04:00 <= time < 09:00 Eastern", text)
        self.assertNotIn("time < 04:15", text)
        self.assertIn("Order activation at or after 09:00 Eastern: 1", text)
        self.assertIn("1 no discovery bars", text)
        self.assertEqual(table_row(text, "TOTAL")[1:], ["1", "0", "1", "0.00%"])

    def test_disabled_late_discovery_keeps_early_cutoff_with_later_entry_deadline(self):
        config = StrategyConfig(late_gap_enabled=False, entry_deadline="09:00")
        text = format_gap_summary([], config)
        self.assertIn("04:00 <= time < 04:15 Eastern", text)
        self.assertNotIn("time < 09:00", text)


if __name__ == "__main__":
    unittest.main()
