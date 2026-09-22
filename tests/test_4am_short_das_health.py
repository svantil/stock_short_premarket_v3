"""DAS connection recovery regressions using memory-only brokers and clocks."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from four_am_short.config import StrategyConfig
from four_am_short.live.config import DasSettings, LiveSettings
from four_am_short.live.das import DasError
from four_am_short.live.engine import LiveEngine
from four_am_short.models import Bar, EASTERN
from test_4am_short_live_engine import FakeBroker, FakeClock, FakeData, FakeFeed


class DasHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.broker = FakeBroker(self.clock)
        self.broker.get_account = Mock(wraps=self.broker.get_account)
        self.feed = FakeFeed()
        self.release_workers = []
        settings = LiveSettings(
            strategy=replace(StrategyConfig(), shares=1000), mode="das_paper",
            state_dir=Path(self.directory.name),
            strategy_config_path=Path(self.directory.name) / "unused-backtest.json",
            das=DasSettings(username="SIMUSER", password="simulation-only", account="SIMTEST"),
        )
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(),
                                 feed=self.feed, now=self.clock)
        await self.engine.open()
        self.engine.running = self.engine.entries_enabled = True
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status.update(ready=True, connected=True, backfill_ready=True)
        self.engine._broker_status.update(connected=True, status="Verified test broker")
        self.engine._broker_check_at = self.clock() + timedelta(hours=1)
        self.engine._locate_service_at = self.clock() + timedelta(hours=1)
        self.engine._closes["TEST"] = 7.0
        self.engine._symbols = ["TEST"]
        self.engine._accept_bar("TEST", Bar(datetime(2026, 9, 21, 4, 0, tzinfo=EASTERN),
                                            9.5, 10, 9, 9.8, 5000), historical=True)
        self.candidate = self.engine._day_state()["candidates"]["TEST"]
        self.quote()

    async def asyncTearDown(self):
        for release in self.release_workers:
            release.set()
        health = getattr(self.engine, "_broker_health_task", None)
        if health:
            await asyncio.gather(health, return_exceptions=True)
        tasks = [getattr(self.engine, name, None) for name in
                 ("_entry_task", "_bootstrap_task", "_finalize_task", "_locate_service_task")]
        tasks += list(self.engine._tasks)
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        self.engine.store.close()
        self.directory.cleanup()

    def quote(self):
        self.engine._quotes["TEST"] = dict(bid=9.2, ask=9.21, timestamp=self.clock().isoformat())

    def block_account_check(self):
        started = asyncio.Event()
        release = threading.Event()
        self.release_workers.append(release)
        loop = asyncio.get_running_loop()

        def check():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=3):
                raise TimeoutError("Test health worker was not released")
            return {"trading_blocked": False}

        self.broker.get_account.side_effect = check
        return started, release

    async def test_health_check_rejects_failure_redacts_details_and_recovers(self):
        self.broker.get_account.side_effect = DasError("Disconnected SIMUSER SIMTEST simulation-only")
        await self.engine._check_broker_health()
        failed = self.engine.snapshot()["broker"]
        self.assertFalse(failed["connected"])
        self.assertFalse(failed["checking"])
        self.assertEqual(failed["last_checked_at"], self.clock().isoformat())
        self.assertIn("Disconnected", failed["last_error"])
        for secret in ("SIMUSER", "SIMTEST", "simulation-only"):
            self.assertNotIn(secret, str(failed))
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=5))
        self.assertFalse(self.engine._can_enter(self.candidate))
        self.assertTrue(self.engine.entries_enabled)

        self.clock.advance(5)
        self.quote()
        self.broker.get_account.side_effect = None
        await self.engine._check_broker_health()
        recovered = self.engine.snapshot()["broker"]
        self.assertTrue(recovered["connected"])
        self.assertFalse(recovered["checking"])
        self.assertFalse(recovered["last_error"])
        self.assertEqual(recovered["last_connected_at"], self.clock().isoformat())
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=10))
        self.assertTrue(self.engine._can_enter(self.candidate))
        self.assertFalse(self.broker.submissions or self.broker.locates)

    async def test_trading_blocked_account_is_not_marked_connected(self):
        self.broker.get_account.return_value = {"trading_blocked": True}
        await self.engine._check_broker_health()
        self.assertFalse(self.engine.snapshot()["broker"]["connected"])
        self.assertFalse(self.engine._can_enter(self.candidate))

    async def test_configured_health_and_retry_intervals_are_used(self):
        self.engine.settings = replace(self.engine.settings, das=replace(
            self.engine.settings.das, health_check_seconds=17, reconnect_seconds=3))
        await self.engine._check_broker_health()
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=17))
        self.broker.get_account.side_effect = DasError("Disconnected")
        await self.engine._check_broker_health()
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=3))

    async def test_snapshot_uses_current_adapter_status_instead_of_stale_connected_flag(self):
        self.broker.connection_status = {
            "connected": False, "checking": False,
            "status": "DAS order server disconnected", "last_error": "DAS order server disconnected",
            "last_checked_at": self.clock().isoformat(), "last_connected_at": None,
        }
        self.assertTrue(self.engine._broker_status["connected"])
        status = self.engine.snapshot()["broker"]
        self.assertFalse(status["connected"])
        self.assertIn("disconnected", status["status"])
        self.assertFalse(self.engine._can_enter(self.candidate))

    async def test_later_adapter_disconnect_wins_over_earlier_successful_health_reply(self):
        self.broker.connection_status = {"connected": True, "last_error": None}

        def account_reply_followed_by_disconnect():
            self.broker.connection_status = {
                "connected": False, "checking": False,
                "last_error": "Order server disconnected after the account reply",
            }
            return {"trading_blocked": False}

        self.broker.get_account.side_effect = account_reply_followed_by_disconnect
        await self.engine._check_broker_health()
        status = self.engine.snapshot()["broker"]
        self.assertFalse(status["connected"])
        self.assertIn("disconnected after", status["last_error"])
        self.assertFalse(self.engine._can_enter(self.candidate))
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=5))

    async def test_periodic_health_check_is_single_background_worker_and_uses_completion_time(self):
        self.engine.entries_enabled = False
        self.engine._broker_check_at = None
        started, release = self.block_account_check()
        await asyncio.wait_for(self.engine.tick(), timeout=0.5)
        worker = self.engine._broker_health_task
        self.assertIsNotNone(worker)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.assertTrue(self.engine.snapshot()["broker"]["checking"])
        self.clock.advance(30)
        for _ in range(3):
            await asyncio.wait_for(self.engine.tick(), timeout=0.5)
        self.assertIs(self.engine._broker_health_task, worker)
        self.assertEqual(self.broker.get_account.call_count, 1)
        release.set()
        await asyncio.wait_for(worker, timeout=1)
        self.assertEqual(self.engine._broker_check_at, self.clock() + timedelta(seconds=10))
        await self.engine.tick()
        self.assertEqual(self.broker.get_account.call_count, 1)
        self.clock.advance(10)
        await self.engine.tick()
        await self.engine._broker_health_task
        self.assertEqual(self.broker.get_account.call_count, 2)

    async def test_failed_health_check_is_retried_only_when_backoff_expires(self):
        self.engine.entries_enabled = False
        self.engine._broker_check_at = None
        self.broker.get_account.side_effect = DasError("Order server not authenticated")
        await self.engine.tick()
        await self.engine._broker_health_task
        self.assertEqual(self.broker.get_account.call_count, 1)
        self.clock.advance(4)
        await self.engine.tick()
        self.assertEqual(self.broker.get_account.call_count, 1)
        self.broker.get_account.side_effect = None
        self.clock.advance(1)
        await self.engine.tick()
        await self.engine._broker_health_task
        self.assertEqual(self.broker.get_account.call_count, 2)
        self.assertTrue(self.engine.snapshot()["broker"]["connected"])

    async def test_start_during_disconnect_keeps_supervisor_and_user_entry_intent(self):
        self.engine.running = self.engine.entries_enabled = False
        self.broker.get_account.side_effect = DasError("Server unavailable")
        with patch.object(self.engine, "_launch_bootstrap"), \
                patch.object(self.engine, "_supervise", new=AsyncMock()):
            await self.engine.start()
        self.assertTrue(self.engine.running)
        self.assertTrue(self.engine.entries_enabled)
        self.assertFalse(self.engine.snapshot()["broker"]["connected"])
        self.assertFalse(self.engine._can_enter(self.candidate))
        self.assertTrue(any(task.get_name() == "4am-supervisor" for task in self.engine._tasks))
        self.assertFalse(self.broker.locates or self.broker.submissions)

    async def test_monitor_and_demo_do_not_probe_injected_broker(self):
        self.engine.entries_enabled = False
        for mode, demo in (("monitor", False), ("das_paper", True)):
            with self.subTest(mode=mode, demo=demo):
                self.engine.settings = replace(self.engine.settings, mode=mode, demo=demo)
                self.engine._broker_check_at = None
                await self.engine.tick()
                await asyncio.sleep(0)
                self.assertIsNone(self.engine._broker_health_task)
                self.broker.get_account.assert_not_called()

    async def test_failed_entry_marks_dashboard_disconnected_and_is_not_replayed_after_recovery(self):
        self.broker.get_account.side_effect = DasError("DAS order server is disconnected or not authenticated")
        await self.engine._enter(self.candidate)
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["status"], "skipped")
        self.assertFalse(self.engine.snapshot()["broker"]["connected"])
        self.assertFalse(self.broker.locates or self.broker.submissions)
        self.broker.get_account.side_effect = None
        await self.engine._check_broker_health()
        await self.engine.tick()
        self.assertEqual(trade["status"], "skipped")
        self.assertIsNone(self.engine._entry_task)
        self.assertFalse(self.broker.locates or self.broker.submissions)

    async def test_known_outage_waits_without_consuming_stock_then_enters_after_recovery(self):
        self.engine._broker_failed(DasError("Disconnected"))
        await self.engine._enter(self.candidate)
        self.assertNotIn("TEST", self.engine._day_state()["trades"])
        self.assertFalse(self.broker.locates or self.broker.submissions)
        await self.engine._check_broker_health()
        await self.engine.tick()
        await self.engine._entry_task
        self.assertEqual(self.engine._day_state()["trades"]["TEST"]["status"], "entry_pending")
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_locate_service_failure_invalidates_dashboard(self):
        self.broker.service_locate_offers = Mock(side_effect=DasError("Locate server disconnected"))
        await self.engine._service_locates()
        self.assertFalse(self.engine.snapshot()["broker"]["connected"])
        self.assertIn("disconnected", self.engine.snapshot()["broker"]["last_error"])

    async def test_managed_order_failure_invalidates_dashboard_and_preserves_order(self):
        await self.engine._enter(self.candidate)
        trade = self.engine._day_state()["trades"]["TEST"]
        token = trade["entry_token"]
        self.broker.lookup_failure = DasError("Order snapshots unavailable")
        await self.engine.tick()
        self.assertFalse(self.engine.snapshot()["broker"]["connected"])
        self.assertEqual(trade["entry_token"], token)
        self.assertTrue(trade["reconciliation_required"])
        self.assertEqual(len(self.broker.submissions), 1)
        self.broker.lookup_failure = None
        await self.engine._check_broker_health()
        await self.engine.tick()
        self.assertFalse(trade["reconciliation_required"])
        self.assertEqual(trade["entry_token"], token)
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_semantic_order_error_does_not_override_verified_adapter_connection(self):
        self.broker.connection_status = {"connected": True, "last_error": None}
        self.engine._broker_failed(DasError("Owned order quantity differs from expected quantity"))
        self.assertTrue(self.engine.snapshot()["broker"]["connected"])

    async def test_stop_entries_does_not_disable_recovery_or_resume_entries(self):
        await self.engine.stop_entries()
        self.engine._broker_failed(DasError("Disconnected"))
        self.clock.advance(5)
        await self.engine.tick()
        await self.engine._broker_health_task
        self.assertTrue(self.engine.running)
        self.assertTrue(self.engine.snapshot()["broker"]["connected"])
        self.assertFalse(self.engine.entries_enabled)
        self.assertFalse(self.engine._can_enter(self.candidate))
        self.assertFalse(self.broker.locates or self.broker.submissions)

    async def test_shutdown_awaits_health_worker_before_releasing_broker(self):
        self.engine.running = False
        started, release = self.block_account_check()
        self.engine._broker_health_task = asyncio.create_task(self.engine._check_broker_health())
        await asyncio.wait_for(started.wait(), timeout=1)
        shutdown = asyncio.create_task(self.engine.close())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            self.assertFalse(self.broker.closed)
        finally:
            release.set()
            await asyncio.wait_for(shutdown, timeout=1)
        self.assertTrue(self.broker.closed)
        self.assertTrue(self.engine._broker_health_task.done())


if __name__ == "__main__":
    unittest.main()
