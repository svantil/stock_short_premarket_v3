"""Final trade statistics use completed trades and P/L after trading costs."""

import csv
import json
import re
import tempfile
import unittest
from pathlib import Path

from four_am_short.config import BacktestConfig, DataConfig, StrategyConfig
from four_am_short.models import TradeResult
from four_am_short.reports import format_trade_summary, summarize, write_reports


METRICS = {
    "Avg winner": "average_winner_net_pnl",
    "Avg loser": "average_loser_net_pnl",
    "Biggest winner": "biggest_winner_net_pnl",
    "Biggest loser": "biggest_loser_net_pnl",
    "Avg trade": "average_net_pnl",
    "Profit factor": "profit_factor",
}


def trade(symbol, pnl, *, day="2026-01-05"):
    return TradeResult(
        day, symbol, "trade", exit_time=f"{day}T05:00:00-05:00",
        exit_reason="time_exit", gross_pnl=pnl + 25,
        commission=10, locate_cost=15, net_pnl=pnl,
    )


def displayed(text, label):
    for line in text.splitlines():
        match = re.fullmatch(re.escape(label) + r"\s*:?\s+(\S+)", line.strip())
        if match:
            return match.group(1)
    raise AssertionError(f"Missing {label!r} in trade summary:\n{text}")


def money(text):
    if not re.fullmatch(r"[+-]?\$\d[\d,]*\.\d{2}", text):
        raise AssertionError(f"Expected a currency amount with cents, got {text!r}")
    return float(text.replace("$", "").replace(",", ""))


class FinalSummaryTests(unittest.TestCase):
    def mixed_rows(self):
        return [
            trade("WIN1", 1300),
            trade("WIN2", 700),
            trade("LOSS", -250),
            trade("COSTLOSS", -20),  # A $5 gross gain becomes a $20 net loss.
            trade("FLAT", 0),
            TradeResult("2026-01-05", "OPEN", "incomplete", net_pnl=100000),
            TradeResult("2026-01-05", "ERROR", "error", net_pnl=-100000),
            TradeResult("2026-01-05", "SKIP", "skipped", net_pnl=100000),
        ]

    def test_costs_determine_winners_and_losers_and_breakevens_count_in_average(self):
        stats = summarize(self.mixed_rows())
        self.assertEqual((stats["trades"], stats["wins"], stats["losses"], stats["breakeven"]), (5, 2, 2, 1))
        expected = {
            "average_winner_net_pnl": 1000,
            "average_loser_net_pnl": -135,
            "biggest_winner_net_pnl": 1300,
            "biggest_loser_net_pnl": -250,
            "average_net_pnl": 346,
            "profit_factor": 7.407407,
        }
        for key, value in expected.items():
            with self.subTest(metric=key):
                self.assertEqual(stats[key], value)

    def test_final_summary_displays_requested_metrics_with_signed_losses(self):
        text = format_trade_summary(self.mixed_rows())
        self.assertIn("Trade summary - 4am short", text)
        expected = {"Avg winner": 1000, "Avg loser": -135, "Biggest winner": 1300,
                    "Biggest loser": -250, "Avg trade": 346}
        for label, value in expected.items():
            with self.subTest(label=label):
                self.assertEqual(money(displayed(text, label)), value)
        self.assertEqual(displayed(text, "Profit factor"), "7.41")
        self.assertTrue(text.splitlines()[-1].strip().startswith("Profit factor"))

    def test_missing_sides_and_no_trades_remain_undefined(self):
        cases = [
            ("wins", [trade("A", 100), trade("B", 300)],
             (200, None, 300, None, 200, None), "inf"),
            ("losses", [trade("A", -100), trade("B", -300)],
             (None, -200, None, -300, -200, 0), "0.00"),
            ("breakeven", [trade("A", 0)],
             (None, None, None, None, 0, None), "--"),
            ("empty", [], (None, None, None, None, None, None), "--"),
        ]
        for name, rows, expected, factor in cases:
            with self.subTest(case=name):
                stats = summarize(rows)
                self.assertEqual(tuple(stats[key] for key in METRICS.values()), expected)
                text = format_trade_summary(rows)
                for (label, _), value in zip(METRICS.items(), expected):
                    if label == "Profit factor":
                        self.assertEqual(displayed(text, label), factor)
                    elif value is None:
                        self.assertEqual(displayed(text, label), "--")
                    else:
                        self.assertEqual(money(displayed(text, label)), value)

    def test_partial_summary_marks_completed_subset(self):
        text = format_trade_summary(self.mixed_rows(), partial=True)
        self.assertIn("Trade summary - 4am short (PARTIAL)", text)
        self.assertEqual(money(displayed(text, "Avg trade")), 346)
        self.assertEqual(displayed(text, "Profit factor"), "7.41")

    def test_saved_report_ends_with_summary_and_exports_period_statistics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = BacktestConfig(root / "input.txt", root / "reports", StrategyConfig(), DataConfig())
            rows = [trade("A", 100), trade("B", -50), trade("C", 900, day="2026-02-02")]
            directory, saved = write_reports(
                config, rows, requested_count=4, interrupted=True,
                input_content=b"2026-01-05 A B\n2026-02-02 C D\n",
            )
            details = (directory / "4am_short_trade_details.txt").read_text()
            expected_footer = format_trade_summary(rows, partial=True)
            self.assertTrue(details.rstrip().endswith(expected_footer))
            self.assertLess(details.index("Monthly summary - 4am short"), details.index("Trade summary - 4am short"))
            persisted = json.loads((directory / "4am_short_summary.json").read_text())
            self.assertEqual(persisted, saved)
            self.assertTrue(persisted["interrupted"])
            self.assertEqual(persisted["statistics"]["average_winner_net_pnl"], 500)
            self.assertEqual(persisted["statistics"]["average_loser_net_pnl"], -50)
            self.assertEqual(persisted["statistics"]["biggest_winner_net_pnl"], 900)
            self.assertEqual(persisted["statistics"]["biggest_loser_net_pnl"], -50)
            self.assertEqual(persisted["statistics"]["average_net_pnl"], 316.666667)
            self.assertEqual(persisted["statistics"]["profit_factor"], 20)
            for period in ("daily", "monthly"):
                with self.subTest(period=period):
                    with (directory / f"4am_short_{period}.csv").open(newline="") as handle:
                        exported = list(csv.DictReader(handle))
                    self.assertEqual(len(exported), 2)
                    self.assertEqual(float(exported[0]["average_winner_net_pnl"]), 100)
                    self.assertEqual(float(exported[0]["average_loser_net_pnl"]), -50)
                    self.assertEqual(float(exported[0]["biggest_winner_net_pnl"]), 100)
                    self.assertEqual(float(exported[0]["biggest_loser_net_pnl"]), -50)
                    self.assertEqual(float(exported[0]["profit_factor"]), 2)
                    self.assertEqual(float(exported[1]["average_winner_net_pnl"]), 900)
                    self.assertEqual(exported[1]["average_loser_net_pnl"], "")
                    self.assertEqual(exported[1]["biggest_loser_net_pnl"], "")
                    self.assertEqual(exported[1]["profit_factor"], "")


if __name__ == "__main__":
    unittest.main()
