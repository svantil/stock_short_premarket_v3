import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from four_am_short.config import load_config, read_alpaca_credentials, read_api_key
from four_am_short.inputs import read_candidates
from four_am_short.models import DataError, TradeResult
from four_am_short.reports import summarize, write_reports


class ConfigAndReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "backtest.json"
        self.raw = {"input_file": "stocks.csv"}
        (self.root / "stocks.csv").write_text("2026-01-05 AAA BBB\n2026-01-05,aaa,CCC # duplicate\n2026-01-02 XYZ\n")

    def config(self):
        self.path.write_text(json.dumps(self.raw))
        return load_config(self.path)

    def test_defaults_paths_and_deduplicated_input(self):
        config = self.config()
        self.assertEqual(config.strategy.shares, 1000)
        self.assertFalse(config.strategy.late_gap_enabled)
        self.assertEqual(config.strategy.late_gap_window_minutes, 15)
        self.assertEqual(config.strategy_name, "4am short")
        self.assertEqual(config.strategy_id, "4am_short")
        self.assertEqual(config.data.provider, "massive")
        self.assertEqual(config.output_dir, self.root / "outcome" / "4am_short")
        self.assertEqual(config.input_file, self.root / "stocks.csv")
        self.assertEqual([(str(c.trading_date), c.symbol) for c in read_candidates(config)], [("2026-01-02", "XYZ"), ("2026-01-05", "AAA"), ("2026-01-05", "BBB"), ("2026-01-05", "CCC")])

    def test_unknown_keys_bad_types_and_invalid_schedule_rejected(self):
        invalid = [
            {"strategy_id": "different_strategy"}, {"strategy_name": "another trade"},
            {"share": 20}, {"shares": True}, {"shares": 0}, {"shares": 10.0},
            {"strategy": {"gap_percent": "30"}}, {"strategy": {"gap_percent": float("nan")}},
            {"strategy": {"entry_deadline": "04:10"}}, {"strategy": {"early_start": "4:00"}},
            {"strategy": {"profit_target_percent": 100}}, {"strategy": {"intrabar_policy": "guess"}},
            {"strategy": {"late_gap_enabled": "true"}}, {"strategy": {"late_gap_enabled": 1}},
            {"strategy": {"late_gap_window_minutes": 0}}, {"strategy": {"late_gap_window_minutes": True}},
            {"strategy": {"late_gap_window_minutes": 15.5}},
            {"data": {"max_retries": -1}}, {"symbols": "AAA"}, {"from_date": "20260101"},
            {"data": {"provider": "iex"}}, {"data": {"provider": []}},
            {"data": {"alpaca_secret_key_env": "secret key"}},
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.raw = {"input_file": "stocks.csv", **value}
                with self.assertRaises(DataError):
                    self.config()

    def test_late_window_parameters_are_loaded_and_snapshotted(self):
        self.raw["strategy"] = {"late_gap_enabled": True, "late_gap_window_minutes": 15,
                                "entry_deadline": "09:00"}
        config = self.config()
        self.assertTrue(config.strategy.late_gap_enabled)
        self.assertEqual(config.snapshot()["strategy"]["late_gap_window_minutes"], 15)

    def test_input_filters_and_precise_error_line(self):
        self.raw.update(from_date="2026-01-05", symbols=["aaa"])
        self.assertEqual([c.symbol for c in read_candidates(self.config())], ["AAA"])
        (self.root / "stocks.csv").write_text("2026-01-05 AAA\n2026-02-30 BBB\n")
        with self.assertRaisesRegex(DataError, "stocks.csv:2"):
            read_candidates(self.config())

    def test_key_only_loader_and_environment_precedence(self):
        config = self.config()
        config.data.env_file.write_text("SHARES=3\nexport MASSIVE_API_KEY='test-key' # note\n")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_api_key(config.data), "test-key")
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "environment-key"}):
            self.assertEqual(read_api_key(config.data), "environment-key")
        self.assertNotIn("test-key", json.dumps(config.snapshot()))

    def test_reports_exclude_unresolved_and_measure_chronological_drawdown(self):
        rows = [
            TradeResult("2026-01-05", "AAA", "trade", exit_time="2026-01-05T05:00:00-05:00", exit_reason="profit_target", gross_pnl=110, commission=10, locate_cost=0, net_pnl=100),
            TradeResult("2026-01-05", "BBB", "trade", exit_time="2026-01-05T04:30:00-05:00", exit_reason="stop_loss", gross_pnl=-45, commission=5, locate_cost=0, net_pnl=-50),
            TradeResult("2026-01-05", "CCC", "incomplete", entry_price=12),
        ]
        summary = summarize(rows)
        self.assertEqual(summary["net_pnl"], 50)
        self.assertEqual(summary["max_closed_trade_drawdown"], 50)
        self.assertEqual(summary["incomplete"], 1)
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["stops"], 1)
        directory, saved = write_reports(self.config(), rows, requested_count=4, interrupted=True)
        self.assertEqual(saved["unprocessed_candidates"], 1)
        self.assertEqual(len((directory / "4am_short_trades.csv").read_text().splitlines()), 3)
        self.assertEqual(len((directory / "4am_short_candidates.csv").read_text().splitlines()), 4)
        self.assertTrue((directory / "4am_short_config.resolved.json").exists())

    def test_alpaca_credentials_aliases_environment_precedence_and_report_provenance(self):
        self.raw["data"] = {"provider": "alpaca"}
        config = self.config()
        config.data.env_file.write_text(
            "ALPACA_API_KEY='file-key'\nALPACA_SECRET_KEY=\"file-secret\"\n"
            "DAS_PASSWORD='unrelated unclosed quote\n")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_alpaca_credentials(config.data), ("file-key", "file-secret"))
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "env-key", "APCA_API_SECRET_KEY": "env-secret"}, clear=True):
            self.assertEqual(read_alpaca_credentials(config.data), ("env-key", "env-secret"))
        folder, report = write_reports(config, [], requested_count=0)
        self.assertEqual(report["data_provider"], "alpaca")
        self.assertEqual(report["data_feed"], "sip")
        self.assertIn("Alpaca SIP", report["assumptions"]["price_data"])
        self.assertIn("Data source: Alpaca SIP", (folder / "4am_short_trade_details.txt").read_text())
        self.assertNotIn("file-secret", json.dumps(config.snapshot()))
        self.assertNotIn("file-key", json.dumps(config.snapshot()))

    def test_custom_alpaca_credential_names_do_not_use_default_aliases(self):
        self.raw["data"] = {"provider": "alpaca", "alpaca_api_key_env": "CUSTOM_KEY",
                            "alpaca_secret_key_env": "CUSTOM_SECRET"}
        config = self.config()
        with patch.dict("os.environ", {"APCA_API_KEY_ID": "alias", "APCA_API_SECRET_KEY": "alias"}, clear=True):
            self.assertEqual(read_alpaca_credentials(config.data), (None, None))
        config.data.env_file.write_text("CUSTOM_KEY=custom-key\nCUSTOM_SECRET=custom-secret\n")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(read_alpaca_credentials(config.data), ("custom-key", "custom-secret"))

    def test_simultaneous_exits_do_not_invent_symbol_order_for_drawdown(self):
        rows = [
            TradeResult("2026-01-05", "AAA", "trade", exit_time="2026-01-05T05:00:00-05:00", gross_pnl=-50, commission=0, locate_cost=0, net_pnl=-50),
            TradeResult("2026-01-05", "BBB", "trade", exit_time="2026-01-05T05:00:00-05:00", gross_pnl=100, commission=0, locate_cost=0, net_pnl=100),
        ]
        self.assertEqual(summarize(rows)["max_closed_trade_drawdown"], 0)


if __name__ == "__main__":
    unittest.main()
