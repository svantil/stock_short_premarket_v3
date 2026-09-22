"""The fast parameter evaluator must preserve reference trade results exactly."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import pickle
import random
import unittest

from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.models import Bar, DataError, EASTERN, PreviousClose
from four_am_short.reentry_sweep_sim import prepare_case, simulate_prepared
from four_am_short.strategy import simulate, simulate_trades


DAY = date(2026, 1, 5)
PREVIOUS = PreviousClose(date(2026, 1, 2), 10)


def bar(clock, opening, high=None, low=None, close=None):
    return Bar(
        datetime.fromisoformat(f"{DAY}T{clock}:00").replace(tzinfo=EASTERN),
        opening, opening if high is None else high,
        opening if low is None else low, opening if close is None else close,
    )


STOPPED = (bar("04:00", 20), bar("04:15", 18), bar("04:16", 18, 25, 17, 24))


class ReentrySweepSimulationTests(unittest.TestCase):
    def assert_equivalent(self, history, config, previous=PREVIOUS):
        reference = simulate_trades(DAY, "TEST", history, previous, config)
        self.assertEqual(len(reference), 2)
        initial_before = asdict(reference[0])
        case = prepare_case(reference[0], history)
        actual = simulate_prepared(case, config)
        self.assertEqual(asdict(actual), asdict(reference[1]))
        self.assertEqual(asdict(reference[0]), initial_before)
        return actual, case

    def test_known_paths_costs_and_cutoff_cases(self):
        following_cases = (
            [bar("04:17", 24), bar("04:18", 14)],
            [bar("04:17", 24), bar("04:18", 29)],
            [bar("04:17", 20, 20.99, 19, 20), bar("04:18", 20, 21, 19, 20), bar("09:20", 20)],
            [bar("04:17", 20), bar("09:20", 24)],
            [bar("04:17", 24), bar("09:20", 23, 40, 10, 20)],
            [bar("04:17", 24), bar("09:19", 23), bar("09:21", 23)],
            [bar("09:19", 20), bar("09:20", 21)],
            [bar("04:17", 20, 21, 10, 20), bar("09:20", 20)],
            [bar("04:17", 24), bar("04:18", 24, 40, 10, 20)],
            [bar("04:17", 24), bar("04:18", 10, 40, 8, 20)],
            [bar("04:17", 24), bar("04:18", 40, 45, 10, 20)],
            [],
        )
        for policy in ("conservative", "ohlc", "olhc"):
            for entry_slippage, exit_slippage in ((0, 0), (100, 100), (2500, 20)):
                config = StrategyConfig(
                    shares=100, intrabar_policy=policy,
                    entry_slippage_bps=entry_slippage, exit_slippage_bps=exit_slippage,
                    commission_per_share_per_side=0.005, locate_fee_per_share=0.1,
                    reentry=ReentryConfig(enabled=True),
                )
                for following in following_cases:
                    with self.subTest(policy=policy, slippage=entry_slippage, following=following):
                        actual, _ = self.assert_equivalent(STOPPED + tuple(following), config)
                        if actual.status == "trade":
                            self.assertEqual(actual.locate_cost, 0)
                            self.assertEqual(actual.commission, 1)

    def test_exact_deadline_next_minute_and_missing_cutoff(self):
        config = StrategyConfig(reentry=ReentryConfig(
            enabled=True, entry_deadline="04:20", time_exit="04:21",
        ))
        for following, status, reason in (
            ([bar("04:19", 21), bar("04:21", 20)], "trade", ""),
            ([bar("04:20", 21), bar("04:21", 20)], "skipped", "entry_not_filled_before_deadline"),
            ([bar("04:19", 21), bar("04:22", 20)], "incomplete", "missing_time_exit_bar"),
        ):
            actual, _ = self.assert_equivalent(STOPPED + tuple(following), config)
            self.assertEqual((actual.status, actual.reason), (status, reason))
        for stopped_at in ("04:19", "04:20", "04:21"):
            history = STOPPED[:2] + (bar(stopped_at, 24), bar("04:22", 30))
            actual, _ = self.assert_equivalent(history, config)
            self.assertEqual(actual.reason, "activation_at_or_after_deadline")
        # A high in the initial stop minute is not available to trade 2.
        config = replace(config, reentry=replace(config.reentry, entry_above_high_percent=25))
        actual, _ = self.assert_equivalent(STOPPED + (bar("04:17", 20), bar("04:21", 20)), config)
        self.assertIsNone(actual.entry_price)

    def test_prepared_case_sorts_filters_copies_and_pickles(self):
        config = StrategyConfig(reentry=ReentryConfig(enabled=True))
        following = [bar("04:17", 24), bar("09:20", 23)]
        history = list(reversed(STOPPED + tuple(following)))
        history.append(replace(bar("04:00", 999), timestamp=bar("04:00", 999).timestamp - timedelta(days=1)))
        history = [replace(item, timestamp=item.timestamp.astimezone(timezone.utc)) for item in history]
        previous = replace(PREVIOUS, source_close=20, split_factor=0.5)
        actual, case = self.assert_equivalent(history, config, previous)
        self.assertEqual(len(case.bars), len(history) - 1)
        self.assertEqual(case.stamps, tuple(sorted(case.stamps)))
        self.assertEqual(case.stamps[case.first_index].strftime("%H:%M"), "04:17")
        self.assertIn("split-normalized", actual.notes)
        restored = pickle.loads(pickle.dumps(case))
        self.assertEqual(simulate_prepared(restored, config), actual)
        initial = simulate(DAY, "TEST", history, previous, config)
        copied = prepare_case(initial, history)
        initial.early_high = 999
        self.assertEqual(copied.initial.early_high, 20)

    def test_randomized_ohlc_equivalence_for_each_policy(self):
        rng = random.Random(49031)
        beginning = bar("04:17", 24).timestamp
        for sample in range(100):
            generated = []
            previous_price = rng.uniform(16, 32)
            for minute in range(44):
                if rng.random() < 0.15:
                    continue  # Sparse aggregates are allowed.
                opening = max(1, previous_price + rng.uniform(-4, 4))
                closing = max(1, opening + rng.uniform(-4, 4))
                low = max(0.1, min(opening, closing) - rng.uniform(0, 5))
                high = max(opening, closing) + rng.uniform(0, 5)
                generated.append(Bar(beginning + timedelta(minutes=minute), opening, high, low, closing))
                previous_price = closing
            for policy in ("conservative", "ohlc", "olhc"):
                config = StrategyConfig(
                    shares=rng.choice((1, 10, 1000)), intrabar_policy=policy,
                    entry_slippage_bps=rng.choice((0, 5, 100, 2500)),
                    exit_slippage_bps=rng.choice((0, 5, 100)),
                    commission_per_share_per_side=rng.choice((0, 0.005)),
                    locate_fee_per_share=rng.choice((0, 0.04)),
                    reentry=ReentryConfig(
                        enabled=True, entry_above_high_percent=rng.choice((0, 2.5, 7.5, 15, 30, 50)),
                        stop_loss_percent=rng.choice((1, 10, 15, 20, 25, 30)),
                        profit_target_percent=rng.choice((1, 20, 30, 40, 50, 60)),
                        entry_deadline=rng.choice(("04:17", "04:20", "04:25", "04:45", "05:00")),
                        time_exit=rng.choice(("05:00", "05:01", "09:20")),
                    ),
                )
                with self.subTest(sample=sample, policy=policy):
                    self.assert_equivalent(STOPPED + tuple(generated), config)

    def test_reuses_preparation_for_many_parameter_combinations(self):
        history = STOPPED + (
            bar("04:17", 20, 24, 16, 22), bar("04:18", 25, 35, 15, 28),
            bar("09:00", 22), bar("09:20", 23),
        )
        base = StrategyConfig(reentry=ReentryConfig(enabled=True))
        initial = simulate(DAY, "TEST", history, PREVIOUS, base)
        case = prepare_case(initial, history)
        for entry in (0, 2.5, 7.5, 15, 30):
            for stop in (10, 20, 30):
                for target in (20, 40, 60):
                    config = replace(base, reentry=replace(
                        base.reentry, entry_above_high_percent=entry,
                        stop_loss_percent=stop, profit_target_percent=target,
                    ))
                    self.assertEqual(
                        simulate_prepared(case, config),
                        simulate_trades(DAY, "TEST", history, PREVIOUS, config)[1],
                    )

    def test_rejects_ineligible_case_and_disabled_retry(self):
        config = StrategyConfig(reentry=ReentryConfig(enabled=True))
        stopped = simulate(DAY, "TEST", STOPPED, PREVIOUS, config)
        for change in (
            {"status": "incomplete"}, {"exit_reason": "time_exit"},
            {"trade_number": 2}, {"early_high": None}, {"exit_time": ""},
        ):
            with self.subTest(change=change), self.assertRaises(DataError):
                prepare_case(replace(stopped, **change), STOPPED)
        case = prepare_case(stopped, STOPPED)
        with self.assertRaises(DataError):
            simulate_prepared(case, StrategyConfig())


if __name__ == "__main__":
    unittest.main()
