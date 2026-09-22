"""Offline regressions for borrowing only as the SIP bid approaches entry."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from four_am_short.config import StrategyConfig
from four_am_short.live.config import DasSettings, LiveSettings, load_live_settings
from four_am_short.live.engine import LiveEngine
from four_am_short.models import Bar, EASTERN
from test_4am_short_live_engine import FakeBroker, FakeClock, FakeData, FakeFeed


class LocateTrackingBroker(FakeBroker):
    """Emulate an offer acceptance that checks eligibility immediately first."""

    def __init__(self, clock):
        super().__init__(clock)
        self.account_checks = []
        self.validity_checks = []
        self.paid_acceptances = 0
        self.account_hook = self.before_accept = self.after_accept = None

    def get_account(self):
        self.account_checks.append("account")
        if self.account_hook:
            self.account_hook()
        return super().get_account()

    def position_qty(self, symbol):
        self.account_checks.append("position")
        return super().position_qty(symbol)

    def list_open_orders(self, symbol):
        self.account_checks.append("orders")
        return super().list_open_orders(symbol)

    def ensure_shortable(self, symbol, shares, max_price, *, still_valid=None):
        self.locates.append((symbol, shares, max_price))
        self.validity_checks.append(still_valid is not None and still_valid())
        if self.before_accept:
            self.before_accept()
        self.validity_checks.append(still_valid is not None and still_valid())
        if not all(self.validity_checks[-2:]):
            return False, "Entry conditions changed before paid acceptance", "", 0.0
        self.paid_acceptances += 1
        if self.after_accept:
            self.after_accept()
        return True, "Simulated paid locate accepted", "LOCATE4", 0.02


class LocateProximityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.broker = LocateTrackingBroker(self.clock)
        settings = LiveSettings(
            strategy=replace(StrategyConfig(), shares=1000), mode="das_paper",
            state_dir=Path(self.directory.name),
            das=DasSettings(username="SIMUSER", password="simulation-only", account="SIMTEST"),
            # Avoid loading any workspace configuration or credential file.
            strategy_config_path=Path(self.directory.name) / "unused-backtest.json",
        )
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(),
                                 feed=FakeFeed(), now=self.clock)
        await self.engine.open()
        self.engine.running = self.engine.entries_enabled = True
        self.engine._broker_status.update(connected=True, status="Verified test broker")
        self.engine._broker_check_at = self.clock() + timedelta(days=1)
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status.update(ready=True, connected=True, backfill_ready=True)
        self.engine._closes["TEST"] = 7.0
        self.engine._symbols = ["TEST"]
        self.engine._accept_bar(
            "TEST", Bar(datetime(2026, 9, 21, 4, 0, tzinfo=EASTERN), 9.5, 10, 9, 9.8, 5000),
            historical=True,
        )
        self.candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(self.candidate["entry_limit"], 9.0)

    async def asyncTearDown(self):
        tasks = [getattr(self.engine, name, None) for name in
                 ("_entry_task", "_bootstrap_task", "_finalize_task", "_locate_service_task", "_broker_health_task")]
        tasks += list(self.engine._tasks)
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        self.engine.store.close()
        self.directory.cleanup()

    async def quote(self, bid, ask=None, *, when=None):
        await self.engine.on_event({
            "T": "q", "S": "TEST", "bp": bid, "ap": bid + 0.01 if ask is None else ask,
            "bs": 100, "as": 100, "t": (when or self.clock()).isoformat(),
        })

    def worker_quote(self, bid):
        # Broker hooks execute synchronously in a worker; emulate an SIP update
        # received during its wait without sleeping or contacting a data service.
        self.engine._quotes["TEST"] = {
            "bid": bid, "ask": bid + 0.01, "timestamp": self.clock().isoformat(),
        }

    async def tick(self):
        await self.engine.tick()
        tasks = [self.engine._entry_task, self.engine._locate_service_task]
        await asyncio.gather(*(task for task in tasks if task))

    def assert_unattempted(self):
        self.assertNotIn("TEST", self.engine._day_state()["trades"])
        self.assertFalse(self.broker.locates)
        self.assertFalse(self.broker.account_checks)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.broker.paid_acceptances, 0)

    async def test_far_bid_waits_without_consuming_day_and_can_bounce_later(self):
        # Ask, midpoint and last trade reach entry, but the executable bid does not.
        await self.quote(8.50, 9.50)
        await self.engine.on_event({"T": "t", "S": "TEST", "p": 9.20,
                                    "t": self.clock().isoformat()})
        await self.tick()
        self.assertEqual(self.candidate["status"], "waiting_for_price")
        self.assertIsNone(self.engine._entry_task)
        await self.engine._enter(self.candidate)  # Scheduling cannot bypass the guard.
        await self.tick()
        self.assert_unattempted()
        saved = json.loads(self.engine.settings.state_path.read_text())
        self.assertNotIn("TEST", saved["days"][str(self.engine._day)]["trades"])

        await self.quote(8.91)
        await self.tick()
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["status"], "entry_pending")
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]["limit_price"], 9.0)

    async def test_exact_one_percent_boundary_is_inclusive(self):
        await self.quote(8.9099)
        await self.tick()
        self.assert_unattempted()
        await self.quote(8.91)
        await self.tick()
        self.assertEqual(self.broker.paid_acceptances, 1)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_bid_already_above_entry_has_no_upper_proximity_bound(self):
        await self.quote(9.50)
        await self.tick()
        self.assertEqual(self.broker.validity_checks, [True, True])
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]["limit_price"], 9.0)

    async def test_stale_near_quote_cannot_start_a_locate(self):
        await self.quote(9.20, when=self.clock() - timedelta(seconds=6))
        await self.tick()
        self.assert_unattempted()
        await self.quote(8.91)
        await self.tick()
        self.assertEqual(len(self.broker.locates), 1)

    async def test_crossed_or_future_near_quote_does_not_replace_far_bid(self):
        await self.quote(8.50)
        await self.quote(9.20, 9.10)
        await self.tick()
        self.assert_unattempted()
        self.assertEqual(self.engine._quotes["TEST"]["bid"], 8.50)
        await self.quote(9.20, when=self.clock() + timedelta(seconds=10))
        await self.tick()
        self.assert_unattempted()
        self.assertEqual(self.engine._quotes["TEST"]["bid"], 8.50)

    async def test_bid_fade_during_account_checks_prevents_borrow(self):
        await self.quote(8.91)
        self.broker.account_hook = lambda: self.worker_quote(8.50)
        await self.tick()
        self.assertFalse(self.broker.locates)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.engine._day_state()["trades"]["TEST"]["status"], "skipped")

    async def test_price_fade_callback_blocks_paid_acceptance_and_is_not_retried(self):
        await self.quote(8.91)
        self.broker.before_accept = lambda: self.worker_quote(8.50)
        await self.tick()
        self.assertEqual(self.broker.validity_checks, [True, False])
        self.assertEqual(self.broker.paid_acceptances, 0)
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.engine._day_state()["trades"]["TEST"]["status"], "skipped")
        self.broker.before_accept = None
        await self.quote(8.91)
        await self.tick()
        await self.engine._enter(self.candidate)
        self.assertEqual(len(self.broker.locates), 1)
        self.assertFalse(self.broker.submissions)

    async def test_price_fade_after_paid_borrow_still_submits_one_resting_entry(self):
        await self.quote(8.91)
        self.broker.after_accept = lambda: self.worker_quote(8.50)
        await self.tick()
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["status"], "entry_pending")
        self.assertEqual(trade["entry_filled_qty"], 0)
        self.assertEqual(self.broker.paid_acceptances, 1)
        self.assertEqual(self.broker.submissions[0]["limit_price"], 9.0)
        self.assertEqual(trade["locate"]["fee_per_share"], 0.02)
        await self.tick()
        await self.engine._enter(self.candidate)
        await self.quote(9.20)
        await self.tick()
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertFalse(self.broker.cancellations)

    async def test_entry_deadline_overrides_near_bid(self):
        self.clock.value = self.clock().replace(hour=6, minute=0)
        await self.quote(9.20)
        await self.tick()
        await self.engine._enter(self.candidate)
        self.assert_unattempted()
        self.assertEqual(self.candidate["status"], "expired")

    async def test_deadline_during_locate_callback_blocks_paid_acceptance(self):
        await self.quote(8.91)

        def reach_deadline():
            self.clock.value = self.clock().replace(hour=6, minute=0)
            self.worker_quote(9.20)

        self.broker.before_accept = reach_deadline
        await self.tick()
        self.assertEqual(self.broker.validity_checks, [True, False])
        self.assertEqual(self.broker.paid_acceptances, 0)
        self.assertFalse(self.broker.submissions)

    async def test_null_restores_legacy_borrow_timing_with_far_bid(self):
        self.engine.settings = replace(self.engine.settings, locate_trigger_below_entry_percent=None)
        await self.quote(8.50, when=self.clock() - timedelta(seconds=6))
        await self.tick()
        self.assert_unattempted()
        await self.quote(8.50)
        await self.tick()
        self.assertEqual(self.broker.paid_acceptances, 1)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]["limit_price"], 9.0)

    async def test_zero_waits_until_bid_reaches_entry(self):
        self.engine.settings = replace(self.engine.settings, locate_trigger_below_entry_percent=0)
        await self.quote(8.9999)
        await self.tick()
        self.assert_unattempted()
        await self.quote(9.0)
        await self.tick()
        self.assertEqual(self.broker.paid_acceptances, 1)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_proximity_does_not_override_high_delay_or_finalized_window(self):
        await self.quote(9.20)
        self.candidate["active_at"] = (self.clock() + timedelta(minutes=1)).isoformat()
        await self.tick()
        self.assert_unattempted()
        self.clock.advance(60)
        await self.quote(9.20)
        self.engine._window_finalized = False
        # Hold the finalizer idle so this test checks the gate independently.
        self.engine._finalize_retry_at = self.clock() + timedelta(minutes=1)
        await self.tick()
        self.assert_unattempted()
        self.engine._window_finalized = True
        await self.tick()
        self.assertEqual(len(self.broker.locates), 1)

    async def test_monitor_delays_activation_and_never_uses_broker(self):
        self.engine.settings = replace(self.engine.settings, mode="monitor")
        broker = Mock()
        self.engine.broker = broker
        await self.quote(8.50)
        await self.tick()
        self.assertNotIn("TEST", self.engine._day_state()["trades"])
        self.assertEqual(self.candidate["status"], "waiting_for_price")
        await self.quote(8.91)
        await self.tick()
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["status"], "entry_pending")
        self.assertEqual(trade["entry_filled_qty"], 0)
        self.assertEqual(trade["locate"]["status"], "not_requested")
        await self.quote(8.50)
        await self.tick()
        self.assertEqual(trade["status"], "entry_pending")
        await self.quote(9.0)
        await self.tick()
        self.assertEqual(trade["entry_avg_price"], 9.0)
        self.assertEqual(trade["entry_filled_qty"], 1000)
        self.assertFalse(broker.mock_calls)

    async def test_snapshot_reports_configured_distance_and_unrounded_trigger(self):
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["rules"]["locate_trigger_below_entry_percent"], 1.0)
        self.assertEqual(snapshot["candidates"][0]["locate_trigger_price"], 8.91)
        self.engine.settings = replace(self.engine.settings, locate_trigger_below_entry_percent=1.5)
        snapshot = self.engine.snapshot()
        self.assertEqual(snapshot["rules"]["locate_trigger_below_entry_percent"], 1.5)
        self.assertEqual(snapshot["candidates"][0]["locate_trigger_price"], 8.865)
        self.engine.settings = replace(self.engine.settings, locate_trigger_below_entry_percent=None)
        snapshot = self.engine.snapshot()
        self.assertIsNone(snapshot["rules"]["locate_trigger_below_entry_percent"])
        self.assertIsNone(snapshot["candidates"][0]["locate_trigger_price"])


class LocateProximityConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.strategy_path = root / "strategy.json"
        self.strategy_path.write_text(json.dumps({"input_file": "unused.txt", "data": {"env_file": None}}))
        self.config_path = root / "live.json"

    def load_execution(self, execution):
        self.config_path.write_text(json.dumps({
            "strategy_config": str(self.strategy_path), "mode": "monitor",
            "env_file": "unused-credentials.txt", "execution": execution,
        }))
        with patch("four_am_short.live.config._environment", return_value={}):
            return load_live_settings(self.config_path)

    def test_omitted_trigger_defaults_to_one_percent(self):
        settings = self.load_execution({})
        self.assertEqual(settings.locate_trigger_below_entry_percent, 1.0)
        self.assertEqual(settings.public_rules()["locate_trigger_below_entry_percent"], 1.0)

    def test_trigger_accepts_zero_finite_percentage_and_null(self):
        for value in (0, 1, 1.5, None):
            with self.subTest(value=value):
                settings = self.load_execution({"locate_trigger_below_entry_percent": value})
                self.assertEqual(settings.locate_trigger_below_entry_percent, value)
                self.assertEqual(settings.public_rules()["locate_trigger_below_entry_percent"], value)

    def test_trigger_rejects_invalid_types_ranges_and_nonfinite_values(self):
        for value in (-0.01, True, False, "1", "null", [], {}, 100, 100.1,
                      float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.load_execution({"locate_trigger_below_entry_percent": value})


if __name__ == "__main__":
    unittest.main()
