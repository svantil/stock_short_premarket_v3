"""Sweep orchestration, validation, and chronological selection regressions.

These use temporary configuration files and synthetic outcomes; no API, broker,
active configuration, or user's historical data is touched.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sweep_4am_short_reentry as runner
from four_am_short.config import BacktestConfig, DataConfig, StrategyConfig
from four_am_short.models import DataError, TradeResult


def spec():
    return {
        "backtest_config": "backtest.json",
        "output_dir": "outcome",
        "training_end_date": "2026-06-30",
        "minimum_training_trades": 2,
        "minimum_full_sample_trades": 3,
        "grid": {
            "entry_above_high_percent": [0, 10, 20],
            "stop_loss_percent": [20],
            "profit_target_percent": [40],
            "entry_deadline": ["09:00"],
            "time_exit": ["09:20"],
        },
    }


def params(offset=0):
    return {key: values[0] for key, values in spec()["grid"].items()} | {
        "entry_above_high_percent": offset,
    }


def trade(day, pnl, *, symbol="TEST", number=2):
    return TradeResult(
        day, symbol, "trade", trade_number=number,
        entry_time=f"{day}T05:00:00-04:00", entry_price=10,
        exit_time=f"{day}T09:20:00-04:00", exit_price=10 - pnl / 100,
        exit_reason="profit_target" if pnl > 0 else "stop_loss",
        shares=100, gross_pnl=pnl, net_pnl=pnl, commission=0, locate_cost=0,
    )


class SweepRunnerTests(unittest.TestCase):
    def write_spec(self, folder, raw=None):
        path = Path(folder) / "sweep.json"
        path.write_text(json.dumps(spec() if raw is None else raw))
        return path

    def test_load_cases_uses_configured_provider_and_offline_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            input_file = Path(folder) / "input.csv"
            input_file.write_text("2026-01-02,TEST\n")
            candidate = SimpleNamespace(trading_date=date(2026, 1, 2), symbol="TEST")
            skipped = TradeResult("2026-01-02", "TEST", "skipped", "synthetic")
            for provider in ("massive", "alpaca"):
                with self.subTest(provider=provider):
                    config = BacktestConfig(input_file, Path(folder), StrategyConfig(),
                                            DataConfig(provider=provider))
                    with patch.object(runner, "create_client") as factory, \
                            patch.object(runner, "read_candidates", return_value=[candidate]), \
                            patch.object(runner, "simulate", return_value=skipped), \
                            contextlib.redirect_stdout(io.StringIO()):
                        cases, initial, sources, content = runner.load_cases(config, True, "09:20")
                    factory.assert_called_once_with(config.data, offline=True)
                    factory.return_value.previous_close.assert_called_once_with("TEST", candidate.trading_date)
                    factory.return_value.minute_bars.assert_called_once_with("TEST", candidate.trading_date)
                    self.assertEqual(initial, [skipped])
                    self.assertEqual(cases, [])
                    self.assertEqual(sources, [])
                    self.assertEqual(content, input_file.read_bytes())

    def test_approved_grid_has_all_5850_valid_combinations_including_equal_cutoffs(self):
        raw = spec()
        raw["grid"] = {
            "entry_above_high_percent": [2.5 * i for i in range(13)],
            "stop_loss_percent": [10, 15, 20, 25, 30],
            "profit_target_percent": [20, 30, 40, 50, 60],
            "entry_deadline": ["06:00", "07:00", "08:00", "09:00", "09:15"],
            "time_exit": ["09:00", "09:10", "09:20", "09:30"],
        }
        with tempfile.TemporaryDirectory() as folder:
            grid = runner.load_grid(self.write_spec(folder, raw))["grid"]
        combinations = runner.combinations(grid, "04:15")
        self.assertEqual(len(combinations), 5850)
        self.assertEqual(len({tuple(row.values()) for row in combinations}), 5850)
        self.assertEqual(len({(row["entry_deadline"], row["time_exit"]) for row in combinations}), 18)
        self.assertIn(params(7.5), combinations)
        self.assertTrue(any(row["entry_deadline"] == row["time_exit"] == "09:00" for row in combinations))
        self.assertTrue(all("04:15" < row["entry_deadline"] <= row["time_exit"] for row in combinations))

    def test_invalid_parameters_and_spec_types_are_rejected(self):
        invalid = []
        for key, values in (
            ("entry_above_high_percent", [-1]), ("entry_above_high_percent", [True]),
            ("stop_loss_percent", [0]), ("stop_loss_percent", ["20"]),
            ("profit_target_percent", [100]), ("profit_target_percent", [float("inf")]),
            ("entry_deadline", ["9:00"]), ("time_exit", ["24:00"]),
            ("time_exit", [False]), ("stop_loss_percent", []),
            ("stop_loss_percent", 20),
        ):
            raw = spec()
            raw["grid"][key] = values
            invalid.append((f"grid {key}={values!r}", raw))
        for key, value in (
            ("minimum_training_trades", True), ("minimum_training_trades", 0),
            ("minimum_full_sample_trades", 2.5), ("training_end_date", "2026-02-30"),
            ("backtest_config", 42), ("backtest_config", ""),
            ("output_dir", []), ("output_dir", ""), ("unknown_option", 1),
        ):
            raw = spec()
            raw[key] = value
            invalid.append((f"spec {key}={value!r}", raw))
        for key in ("backtest_config", "training_end_date"):
            raw = spec()
            del raw[key]
            invalid.append((f"missing {key}", raw))
        raw = spec()
        del raw["grid"]["stop_loss_percent"]
        invalid.append(("missing grid parameter", raw))
        raw = spec()
        raw["grid"]["unknown_parameter"] = [1]
        invalid.append(("unknown grid parameter", raw))
        invalid.extend((("top-level list", []), ("null grid", spec() | {"grid": None})))
        with tempfile.TemporaryDirectory() as folder:
            for name, raw in invalid:
                with self.subTest(name=name), self.assertRaises(DataError):
                    runner.load_grid(self.write_spec(folder, raw))

    def test_duplicate_grid_values_do_not_duplicate_experiments(self):
        raw = spec()
        raw["grid"]["entry_above_high_percent"] = [0, 10, 10.0, 0, 20]
        raw["grid"]["entry_deadline"] = ["09:00", "09:00"]
        with tempfile.TemporaryDirectory() as folder:
            loaded = runner.load_grid(self.write_spec(folder, raw))
        self.assertEqual(len(runner.combinations(loaded["grid"], "04:15")), 3)

    def test_evaluation_includes_split_day_in_training_and_retains_unresolved_counts(self):
        rows = [
            trade("2026-06-29", 100), trade("2026-06-30", -25),
            trade("2026-07-01", 200),
            TradeResult("2026-07-02", "SKIP", "skipped", trade_number=2),
            TradeResult("2026-07-03", "OPEN", "incomplete", trade_number=2),
            TradeResult("2026-07-06", "ERROR", "error", trade_number=2),
        ]
        strategy = StrategyConfig(shares=71, commission_per_share_per_side=0.01)
        with patch.object(runner, "simulate_prepared", side_effect=rows) as simulate:
            result, details = runner.evaluate(list(range(len(rows))), strategy, params(10), "2026-06-30")
        self.assertEqual(details, rows)
        self.assertEqual(result["train_trades"], 2)
        self.assertEqual(result["train_net_pnl"], 75)
        self.assertEqual(result["validation_trades"], 1)
        self.assertEqual(result["validation_net_pnl"], 200)
        self.assertEqual(result["all_trades"], 3)
        self.assertEqual(result["all_net_pnl"], 275)
        for key in ("skipped", "incomplete", "errors"):
            self.assertEqual(result[f"validation_{key}"], 1)
            self.assertEqual(result[f"train_{key}"], 0)
        passed_config = simulate.call_args.args[1]
        self.assertTrue(passed_config.reentry.enabled)
        self.assertEqual(passed_config.reentry.entry_above_high_percent, 10)
        self.assertEqual(passed_config.shares, 71)
        self.assertEqual(passed_config.commission_per_share_per_side, 0.01)
        self.assertFalse(strategy.reentry.enabled)

    def test_training_rank_ignores_later_results_and_uses_stated_ties(self):
        base = params() | {
            "train_net_pnl": 100, "train_max_closed_trade_drawdown": 30,
            "train_trades": 20, "validation_net_pnl": -1000,
        }
        later_better = base | {"validation_net_pnl": 1000000}
        self.assertEqual(runner.ranking_key(base, "train"), runner.ranking_key(later_better, "train"))
        lower_profit = base | {"train_net_pnl": 99, "train_max_closed_trade_drawdown": 0}
        less_drawdown = base | {"train_max_closed_trade_drawdown": 20}
        more_fills = base | {"train_trades": 21}
        different_offset = base | {"entry_above_high_percent": 10}
        ranked = sorted([lower_profit, base, different_offset, more_fills, less_drawdown],
                        key=lambda row: runner.ranking_key(row, "train"))
        self.assertEqual(ranked, [less_drawdown, more_fills, base, different_offset, lower_profit])

    def setup_synthetic_main(self, folder):
        folder = Path(folder)
        config_path = folder / "backtest.json"
        config_path.write_text(json.dumps({
            "input_file": "input.csv", "shares": 100,
            "strategy": {"reentry": {"enabled": True, **params()}},
            "data": {"env_file": None},
        }))
        return self.write_spec(folder), config_path

    def test_end_to_end_reports_select_training_independently_and_preserve_config(self):
        days = ["2026-06-29", "2026-06-30", "2026-07-01"]
        cases = [SimpleNamespace(initial=trade(day, -30, number=1), index=i) for i, day in enumerate(days)]
        initial = [case.initial for case in cases] + [
            TradeResult("2026-07-02", "MISS", "error", notes="synthetic missing data"),
            TradeResult("2026-07-03", "OPEN", "incomplete", "missing_time_exit_bar"),
        ]
        sources = [(date.fromisoformat(day), "TEST", [], None) for day in days]
        input_bytes = b"2026-06-29,TEST\n2026-06-30,TEST\n2026-07-01,TEST\n"

        def outcome(case, strategy):
            offset = strategy.reentry.entry_above_high_percent
            if offset == 20 and case.index == 1:
                return TradeResult(case.initial.date, "TEST", "incomplete", trade_number=2)
            values = {0: (10, 10, -100), 10: (5, 5, 100), 20: (10000, 0, 10000)}
            return trade(case.initial.date, values[offset][case.index])

        def reference(day, symbol, bars, previous, strategy):
            case = cases[days.index(day.isoformat())]
            return [case.initial, outcome(case, strategy)]

        with tempfile.TemporaryDirectory() as folder:
            spec_path, config_path = self.setup_synthetic_main(folder)
            original = config_path.read_bytes()
            output = Path(folder) / "result"
            with patch.object(runner, "load_cases", return_value=(cases, initial, sources, input_bytes)) as load, \
                    patch.object(runner, "simulate_prepared", side_effect=outcome), \
                    patch.object(runner, "simulate_trades", side_effect=reference), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = runner.main(["--config", str(spec_path), "--offline", "--output-dir", str(output)])
            self.assertEqual(result, 0)
            self.assertTrue(load.call_args.args[1])
            self.assertEqual(load.call_args.args[2], "09:30")
            self.assertEqual(config_path.read_bytes(), original)
            manifest = json.loads((output / "sweep_manifest.json").read_text())
            self.assertEqual(manifest["data_provider"], "massive")
            self.assertEqual(manifest["grid_combinations"], 3)
            self.assertEqual(manifest["training_stop_out_cases"], 2)
            self.assertEqual(manifest["validation_stop_out_cases"], 1)
            self.assertEqual(manifest["data_exclusion_count"], 2)
            self.assertEqual(manifest["training_eligible_combinations"], 2)
            self.assertEqual(manifest["full_sample_eligible_combinations"], 2)
            self.assertEqual(manifest["input_sha256"], hashlib.sha256(input_bytes).hexdigest())
            self.assertTrue(manifest["configuration_unchanged"])
            comparisons = manifest["comparisons"]
            self.assertEqual(comparisons["training_winner"]["parameters"]["entry_above_high_percent"], 0)
            self.assertEqual(comparisons["full_sample_winner"]["parameters"]["entry_above_high_percent"], 10)
            self.assertEqual(comparisons["training_winner"]["reentry"]["net_pnl"], -80)
            self.assertEqual(comparisons["training_winner"]["combined"]["net_pnl"], -170)
            with (output / "all_combinations.csv").open() as handle:
                combinations = list(csv.DictReader(handle))
            self.assertEqual(len(combinations), 3)
            unresolved = next(row for row in combinations if float(row["entry_above_high_percent"]) == 20)
            self.assertEqual(unresolved["training_selection_eligible"], "False")
            self.assertEqual(unresolved["full_sample_selection_eligible"], "False")
            self.assertEqual(unresolved["train_rank"], "")
            self.assertEqual(unresolved["full_sample_rank"], "")
            self.assertEqual((output / "input.snapshot.txt").read_bytes(), input_bytes)
            for slug in ("baseline", "training_winner", "full_sample_winner"):
                saved = json.loads((output / f"{slug}_backtest.json").read_text())
                self.assertTrue(saved["strategy"]["reentry"]["enabled"])
                self.assertTrue((output / f"{slug}_monthly.csv").exists())
                self.assertTrue((output / f"{slug}_trades.csv").exists())
            self.assertIn("All P/L below is **re-entry only**", (output / "sweep_report.md").read_text())
            with patch.object(runner, "load_cases") as no_load, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(DataError):
                    runner.main(["--config", str(spec_path), "--offline", "--output-dir", str(output)])
                no_load.assert_not_called()

    def test_validate_only_does_not_request_data_or_create_output(self):
        with tempfile.TemporaryDirectory() as folder:
            spec_path, config_path = self.setup_synthetic_main(folder)
            output = Path(folder) / "result"
            with patch.object(runner, "load_cases") as no_load, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(["--config", str(spec_path), "--validate-only", "--output-dir", str(output)]), 0)
            no_load.assert_not_called()
            self.assertFalse(output.exists())

    def test_no_valid_combinations_fail_before_data_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            spec_path, _ = self.setup_synthetic_main(folder)
            raw = spec()
            raw["grid"]["entry_deadline"] = ["04:15", "10:00"]
            self.write_spec(folder, raw)
            with patch.object(runner, "load_cases") as no_load, self.assertRaises(DataError):
                runner.main(["--config", str(spec_path), "--validate-only"])
            no_load.assert_not_called()

    def test_existing_partial_sweep_artifact_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            spec_path, _ = self.setup_synthetic_main(folder)
            output = Path(folder) / "partial"
            output.mkdir()
            saved = output / "initial_candidates.csv"
            saved.write_text("existing partial results\n")
            with patch.object(runner, "load_cases") as no_load, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(DataError):
                    runner.main(["--config", str(spec_path), "--offline", "--output-dir", str(output)])
            no_load.assert_not_called()
            self.assertEqual(saved.read_text(), "existing partial results\n")

    def test_no_stopped_trades_saves_coverage_before_failing(self):
        initial = [TradeResult("2026-06-30", "MISS", "error", "market_data_error")]
        with tempfile.TemporaryDirectory() as folder:
            spec_path, _ = self.setup_synthetic_main(folder)
            output = Path(folder) / "empty_result"
            with patch.object(runner, "load_cases", return_value=([], initial, [], b"2026-06-30,MISS\n")), \
                    patch.object(runner, "evaluate") as no_evaluate, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(DataError):
                    runner.main(["--config", str(spec_path), "--offline", "--output-dir", str(output)])
            no_evaluate.assert_not_called()
            coverage = json.loads((output / "data_exclusions.json").read_text())
            self.assertEqual(coverage[0]["symbol"], "MISS")
            self.assertEqual(json.loads((output / "initial_statistics.json").read_text())["errors"], 1)
            self.assertFalse((output / "sweep_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
