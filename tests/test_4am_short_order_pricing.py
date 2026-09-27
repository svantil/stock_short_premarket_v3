"""Historical entries use the same executable limit precision as live orders."""

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest

from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.models import Bar, EASTERN, PreviousClose
from four_am_short.pricing import order_price
from four_am_short.reentry_sweep_sim import prepare_case, simulate_prepared
from four_am_short.strategy import simulate, simulate_trades
from four_am_short.live.config import LiveSettings
from four_am_short.live.engine import LiveEngine


DAY = date(2026, 1, 5)


def bar(clock, opening, high=None, low=None, close=None):
    return Bar(
        datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
        opening, opening if high is None else high,
        opening if low is None else low, opening if close is None else close,
    )


class OrderPricingTests(unittest.TestCase):
    def test_order_limits_round_up_without_changing_exact_ticks(self):
        for price, expected in (
            (0.00001, 0.0001), (0.9999, 0.9999), (0.99991, 1),
            (1, 1), (1.00001, 1.01), (3.969, 3.97),
            (9.9999, 10), (10, 10), (10.00001, 10.01),
        ):
            with self.subTest(price=price):
                self.assertEqual(order_price(price), expected)

    def test_initial_order_waits_for_rounded_limit_then_keeps_threshold_precision(self):
        history = [
            bar("04:00", 4.41),
            bar("04:15", 3.96, 3.969, 3.96, 3.96),
            bar("04:16", 3.96, 3.97, 3.96, 3.96),
            bar("09:30", 3.9),
        ]
        result = simulate(DAY, "TEST", history, PreviousClose(date(2026, 1, 2), 2), StrategyConfig())
        self.assertEqual(result.entry_limit, 3.97)
        self.assertEqual(result.entry_price, 3.97)
        self.assertEqual(result.entry_time, "2026-01-05T04:16:00-05:00")
        self.assertEqual(result.stop_price, 5.161)
        self.assertEqual(result.target_price, 3.47375)

    def test_initial_bounds_apply_to_the_rounded_limit(self):
        config = StrategyConfig(min_entry_price=1, max_entry_price=10)
        for high, limit, filled in (
            (1.111, 0.9999, False),
            (1.1111, 1, True),
            (11.111, 10, True),
            (11.112, 10.01, False),
        ):
            with self.subTest(high=high):
                history = [bar("04:00", high), bar("04:15", limit), bar("09:30", limit)]
                result = simulate(
                    DAY, "TEST", history,
                    PreviousClose(date(2026, 1, 2), high / 2), config,
                )
                self.assertEqual(result.entry_limit, limit)
                self.assertEqual(result.entry_price, limit if filled else None)
                self.assertEqual(result.reason, "" if filled else "entry_price_out_of_range")

    def test_reentry_and_sweep_wait_for_the_rounded_limit(self):
        config = StrategyConfig(reentry=ReentryConfig(enabled=True))
        history = [
            bar("04:00", 4.41), bar("04:15", 3.97),
            bar("04:16", 3.97, 5.2, 3.97, 5.2),
            bar("04:17", 4.63, 4.639, 4.63, 4.63),
            bar("04:18", 4.63, 4.64, 4.63, 4.63),
            bar("09:20", 4.5),
        ]
        initial, retry = simulate_trades(
            DAY, "TEST", history, PreviousClose(date(2026, 1, 2), 2), config,
        )
        self.assertEqual(initial.exit_reason, "stop_loss")
        self.assertEqual(retry.entry_limit, 4.64)
        self.assertEqual(retry.entry_price, 4.64)
        self.assertEqual(retry.entry_time, "2026-01-05T04:18:00-05:00")
        self.assertEqual(retry.stop_price, 5.568)
        self.assertEqual(retry.target_price, 2.784)
        self.assertEqual(simulate_prepared(prepare_case(initial, history), config), retry)

    def test_reentry_and_sweep_bounds_use_the_rounded_limit(self):
        config = StrategyConfig(
            min_entry_price=1, max_entry_price=10,
            reentry=ReentryConfig(enabled=True),
        )
        for high, limit, filled in ((9.5238, 10, True), (9.5239, 10.01, False)):
            with self.subTest(high=high):
                history = [
                    bar("04:00", high), bar("04:15", 8.58),
                    bar("04:16", 8.58, 12, 8.58, 12),
                    bar("04:17", 10), bar("09:20", 9.9),
                ]
                initial, retry = simulate_trades(
                    DAY, "TEST", history, PreviousClose(date(2026, 1, 2), 4), config,
                )
                self.assertEqual(initial.exit_reason, "stop_loss")
                self.assertEqual(retry.entry_limit, limit)
                self.assertEqual(retry.entry_price, 10 if filled else None)
                self.assertEqual(retry.reason, "" if filled else "entry_price_out_of_range")
                self.assertEqual(simulate_prepared(prepare_case(initial, history), config), retry)

    def test_below_dollar_order_keeps_four_decimals_with_no_entry_band(self):
        config = replace(StrategyConfig(), entry_below_high_percent=10)
        result = simulate(
            DAY, "TEST", [bar("04:00", 0.5555), bar("04:15", 0.5), bar("09:30", 0.49)],
            PreviousClose(date(2026, 1, 2), 0.25), config,
        )
        self.assertEqual(result.entry_limit, 0.5)
        self.assertEqual(result.entry_price, 0.5)

    def test_live_candidate_and_backtest_use_identical_rounded_initial_limits(self):
        config = StrategyConfig(min_entry_price=1, max_entry_price=10)
        with tempfile.TemporaryDirectory() as directory:
            for high in (1.111, 1.1111, 4.41, 11.111, 11.112):
                with self.subTest(high=high):
                    engine = LiveEngine(
                        LiveSettings(strategy=config, state_dir=Path(directory)),
                        now=lambda: bar("04:30", 1).timestamp,
                    )
                    engine._closes = {"TEST": high / 2}
                    engine._symbols = ["TEST"]
                    setup = bar("04:00", high)
                    engine._accept_bar("TEST", setup, historical=True)
                    candidate = engine._day_state()["candidates"]["TEST"]
                    result = simulate(
                        DAY, "TEST", [setup],
                        PreviousClose(date(2026, 1, 2), high / 2), config,
                    )
                    self.assertEqual(candidate["entry_limit"], result.entry_limit)
                    self.assertEqual(candidate["active_at"], result.order_active_time)
                    self.assertEqual(
                        engine._entry_price_block_reason(candidate["entry_limit"], "Entry limit") is not None,
                        result.reason == "entry_price_out_of_range",
                    )

    def test_live_reentry_limit_matches_both_backtest_paths(self):
        config = StrategyConfig(reentry=ReentryConfig(enabled=True))
        history = [
            bar("04:00", 4.41), bar("04:15", 3.97),
            bar("04:16", 3.97, 5.2, 3.97, 5.2), bar("04:17", 4.64), bar("09:20", 4.5),
        ]
        initial, retry = simulate_trades(
            DAY, "TEST", history, PreviousClose(date(2026, 1, 2), 2), config,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = LiveEngine(
                LiveSettings(strategy=config, state_dir=Path(directory)),
                now=lambda: bar("04:30", 1).timestamp,
            )
            primary = {
                "symbol": "TEST", "date": str(DAY), "status": "closed",
                "exit_reason": "stop_loss", "entry_filled_qty": config.shares,
                "remaining_qty": 0, "entry_terminal": True,
                "early_high": initial.early_high, "exit_time": initial.exit_time,
                "requested_qty": config.shares,
            }
            candidate = engine._reentry_candidate(primary)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["entry_limit"], 4.64)
            self.assertEqual(candidate["entry_limit"], retry.entry_limit)
            self.assertEqual(simulate_prepared(prepare_case(initial, history), config), retry)


if __name__ == "__main__":
    unittest.main()
