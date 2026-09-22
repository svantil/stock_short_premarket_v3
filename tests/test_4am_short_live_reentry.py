"""Offline re-entry lifecycle checks; no sockets, broker orders or paid locates."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from four_am_short.config import ReentryConfig, StrategyConfig
from four_am_short.live.config import DasSettings, LiveSettings
from four_am_short.live.das import DasError
from four_am_short.live.engine import LiveEngine
from four_am_short.models import Bar, EASTERN
from test_4am_short_live_engine import FakeBroker, FakeClock, FakeData, FakeFeed


class LiveReentryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.broker, self.feed = FakeBroker(self.clock), FakeFeed()
        self.broker.validate_shortable = Mock(return_value=(True, "Existing located shares available"))
        settings = LiveSettings(
            strategy=replace(StrategyConfig(), reentry=ReentryConfig(enabled=True)),
            mode="das_paper", state_dir=Path(self.directory.name),
            das=DasSettings(username="SIMUSER", password="simulation-only", account="SIMTEST"),
            locate_trigger_below_entry_percent=0)
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(), feed=self.feed, now=self.clock)
        await self.engine.open()
        self.ready()
        self.engine._accept_bar("TEST", Bar(datetime(2026, 9, 21, 4, 0, tzinfo=EASTERN),
                                            9.5, 10, 9, 9.8, 5000), historical=True)
        self.candidate = self.engine._day_state()["candidates"]["TEST"]
        self.quote(9.49, 9.50)

    def ready(self):
        self.engine.running = self.engine.entries_enabled = True
        self.engine._broker_status.update(connected=True, status="Verified test broker")
        self.engine._broker_check_at = self.clock() + timedelta(days=1)
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status.update(ready=True, connected=True, backfill_ready=True)
        self.engine._closes = {"TEST": 7.0}
        self.engine._symbols = ["TEST"]

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

    def quote(self, bid, ask):
        self.engine._quotes["TEST"] = dict(bid=bid, ask=ask, timestamp=self.clock().isoformat())

    def child(self):
        return self.engine._day_state()["trades"].get("TEST:reentry")

    async def entered_primary(self):
        await self.engine._enter(self.candidate)
        primary = self.engine._day_state()["trades"]["TEST"]
        self.broker.fill(primary["entry_token"], 1000, 9.5)
        await self.engine._manage_broker_trade(primary)
        return primary

    async def closed_primary(self, reason="stop_loss"):
        primary = await self.entered_primary()
        primary["exit_reason"] = reason
        self.quote(12.39, 12.40)
        await self.engine._manage_broker_trade(primary)
        cover = primary["cover_orders"][0]
        self.broker.fill(cover["token"], 1000, 12.4)
        await self.engine._manage_broker_trade(primary)
        self.assertEqual(primary["status"], "closed")
        return primary

    async def enter_reentry(self, primary):
        candidate = self.engine._reentry_candidate(primary)
        self.assertIsNotNone(candidate)
        await self.engine._enter(candidate)
        return self.child()

    async def test_stop_creates_second_intent_using_original_high_and_existing_borrow_only(self):
        primary = await self.closed_primary()
        self.candidate["early_high"] = 99  # An unrelated later candidate edit cannot change the saved high.
        child = await self.enter_reentry(primary)
        self.assertEqual(child["trade_number"], 2)
        self.assertEqual(primary["trade_number"], 1)
        self.assertEqual(child["early_high"], 10)
        self.assertEqual(child["entry_limit"], 10.5)
        self.assertNotEqual(child["entry_token"], primary["entry_token"])
        self.assertEqual(child["stop_percent"], 20)
        self.assertEqual(child["target_percent"], 40)
        self.assertTrue(child["entry_deadline"].endswith("09:20:00-04:00"))
        self.assertTrue(child["time_exit"].endswith("09:20:00-04:00"))
        self.assertEqual(len(self.broker.locates), 1)
        self.broker.validate_shortable.assert_called_once_with("TEST", 1000)
        self.assertEqual(child["locate"]["status"], "reused")
        self.assertEqual(child["locate"]["fee_per_share"], 0)
        self.assertEqual(primary["locate"]["status"], "available")
        self.assertEqual(primary["status"], "closed")
        self.assertEqual(len(self.engine.snapshot()["trades"]), 2)
        self.assertEqual(self.engine.snapshot()["candidates"][0]["trade_number"], 2)
        self.assertGreater(child["active_at"], self.candidate["active_at"])
        self.assertEqual(child["active_at"], primary["exit_time"])
        self.assertEqual(self.engine.snapshot()["candidates"][0]["active_at"], child["active_at"])
        child.pop("active_at")  # Existing persisted children still show their primary stop time.
        self.assertEqual(self.engine.snapshot()["candidates"][0]["active_at"], primary["exit_time"])

    async def test_reentry_limit_is_submitted_even_far_below_entry_and_after_primary_deadline(self):
        primary = await self.closed_primary()
        self.clock.value = self.clock.value.replace(hour=8)
        self.quote(7, 7.01)
        await self.engine._sync_subscriptions()
        self.assertEqual(self.feed.symbols, {"TEST"})
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "entry_pending")
        self.assertEqual(self.broker.submissions[-1]["limit_price"], 10.5)
        self.assertEqual(len(self.broker.locates), 1)

    async def test_tick_automatically_submits_second_order_once_primary_cover_is_confirmed(self):
        primary = await self.closed_primary()
        await self.engine.tick()
        await self.engine._entry_task
        self.assertEqual(self.child()["status"], "entry_pending")
        await self.engine.tick()
        self.assertEqual(len([row for row in self.broker.submissions if row["side"] == "sell"]), 2)
        self.assertEqual(primary["status"], "closed")

    async def test_disabled_and_non_stop_exits_do_not_qualify(self):
        primary = await self.closed_primary()
        for reason in ("profit_target", "time_exit", "manual_cover", "shutdown", "late_entry_fill"):
            primary["exit_reason"] = reason
            self.assertIsNone(self.engine._reentry_candidate(primary), reason)
        primary["exit_reason"] = "stop_loss"
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, reentry=ReentryConfig(enabled=False)))
        self.assertIsNone(self.engine._reentry_candidate(primary))
        await self.engine.tick()
        self.assertIsNone(self.child())
        self.assertEqual(len(self.broker.submissions), 2)  # Original sell and stop cover.

    async def test_partial_stop_waits_for_full_cover_and_terminal_orders(self):
        primary = await self.entered_primary()
        self.quote(12.39, 12.40)
        await self.engine._manage_broker_trade(primary)
        self.assertIsNone(self.engine._reentry_candidate(primary))
        cover = primary["cover_orders"][0]
        self.broker.fill(cover["token"], 500, 12.4)
        await self.engine._manage_broker_trade(primary)
        self.assertIsNone(self.engine._reentry_candidate(primary))
        self.broker.fill(cover["token"], 1000, 12.4)
        await self.engine._manage_broker_trade(primary)
        self.assertIsNotNone(self.engine._reentry_candidate(primary))
        for field, value in (("entry_terminal", False), ("reconciliation_required", True),
                             ("remaining_qty", 1), ("status", "uncertain")):
            original = primary[field]
            primary[field] = value
            self.assertIsNone(self.engine._reentry_candidate(primary), field)
            primary[field] = original
        cover["status"] = "partially_filled"
        self.assertIsNone(self.engine._reentry_candidate(primary))

    async def test_partial_original_entry_cancels_before_stop_and_reentry(self):
        await self.engine._enter(self.candidate)
        primary = self.engine._day_state()["trades"]["TEST"]
        self.broker.fill(primary["entry_token"], 300, 9.5)
        self.quote(12.39, 12.40)
        await self.engine._manage_broker_trade(primary)
        self.assertIsNone(self.engine._reentry_candidate(primary))
        self.assertFalse(primary["cover_orders"])
        self.broker.acknowledge_cancel(primary["entry_token"])
        await self.engine._manage_broker_trade(primary)
        cover = primary["cover_orders"][0]
        self.broker.fill(cover["token"], 300, 12.4)
        await self.engine._manage_broker_trade(primary)
        child = await self.enter_reentry(primary)
        self.assertEqual(child["requested_qty"], 1000)
        self.assertEqual(primary["entry_filled_qty"], 300)
        self.assertEqual(len(self.broker.locates), 1)

    async def test_unavailable_reuse_is_skipped_without_purchase_or_retry(self):
        primary = await self.closed_primary()
        self.broker.validate_shortable.return_value = (False, "Existing borrow is insufficient")
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "skipped")
        self.assertIn("no new locate purchase", child["note"])
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertIsNone(self.engine._reentry_candidate(primary))

    async def test_borrow_check_failure_never_calls_purchase(self):
        primary = await self.closed_primary()
        self.broker.validate_shortable.side_effect = DasError("Read-only borrow check unavailable")
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "skipped")
        self.assertIn("borrow check unavailable", child["note"])
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_stale_quote_pause_and_readiness_prevent_reentry(self):
        primary = await self.closed_primary()
        candidate = self.engine._reentry_candidate(primary)
        self.clock.advance(20)
        await self.engine._enter(candidate)
        self.assertIsNone(self.child())
        self.quote(12.39, 12.4)
        self.engine.entries_enabled = False
        await self.engine._enter(candidate)
        self.assertIsNone(self.child())
        self.engine.entries_enabled = True
        self.engine._data_status["ready"] = False
        await self.engine._enter(candidate)
        self.assertIsNone(self.child())
        self.broker.validate_shortable.assert_not_called()

    async def test_fresh_quote_from_before_flat_confirmation_cannot_start_reentry(self):
        primary = await self.entered_primary()
        self.quote(12.39, 12.40)
        await self.engine._manage_broker_trade(primary)
        self.clock.advance(1)  # The stop cover finishes after the last available quote.
        self.broker.fill(primary["cover_orders"][0]["token"], 1000, 12.4)
        await self.engine._manage_broker_trade(primary)
        self.assertIsNotNone(self.engine._fresh_quote("TEST"))
        candidate = self.engine._reentry_candidate(primary)
        await self.engine._enter(candidate)
        self.assertIsNone(self.child())
        self.broker.validate_shortable.assert_not_called()
        self.quote(12.39, 12.40)
        await self.engine._enter(candidate)
        self.assertEqual(self.child()["status"], "entry_pending")

    async def test_quote_before_actual_reentry_fill_cannot_latch_price_exit(self):
        primary = await self.closed_primary()
        child = await self.enter_reentry(primary)
        self.quote(7.4, 7.5)
        self.clock.advance(1)
        self.broker.fill(child["entry_token"], 1000, 12.5)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["target_price"], 7.5)
        self.assertEqual(child["exit_reason"], "")
        self.assertFalse(child["cover_orders"])
        self.quote(7.4, 7.5)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["exit_reason"], "profit_target")
        self.assertEqual(child["cover_orders"][0]["qty"], 1000)

    async def test_flat_book_is_rechecked_before_reentry(self):
        primary = await self.closed_primary()
        self.broker.position_override = -1
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "skipped")
        self.assertIn("Existing broker position/order", child["note"])
        self.broker.validate_shortable.assert_not_called()
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_primary_broker_order_history_must_still_be_terminal_and_match(self):
        primary = await self.closed_primary()
        self.broker.orders[primary["entry_token"]]["filled_qty"] = 999
        self.broker.position_override = 0  # Flat alone is not proof of matching fills.
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "skipped")
        self.assertIn("fills changed", child["note"])
        self.broker.validate_shortable.assert_not_called()
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_time_expiring_during_borrow_check_prevents_wire_submission(self):
        primary = await self.closed_primary()
        def expire(*args):
            self.clock.value = self.clock.value.replace(hour=9, minute=20)
            self.quote(12.39, 12.4)
            return True, "Existing borrow available"
        self.broker.validate_shortable.side_effect = expire
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "skipped")
        self.assertEqual(len(self.broker.submissions), 2)
        self.assertEqual(len(self.broker.locates), 1)

    async def test_pending_order_cancels_at_independent_deadline(self):
        primary = await self.closed_primary()
        child = await self.enter_reentry(primary)
        self.clock.value = self.clock.value.replace(hour=9, minute=20)
        self.quote(11, 11.01)
        await self.engine._manage_broker_trade(child)
        self.assertIn(child["entry_order_id"], self.broker.cancellations)
        self.assertFalse(child["entry_terminal"])
        self.broker.acknowledge_cancel(child["entry_token"])
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["status"], "skipped")
        self.assertIsNone(self.engine._reentry_candidate(primary))

    async def test_exact_deadline_never_creates_child(self):
        primary = await self.closed_primary()
        self.clock.value = self.clock.value.replace(hour=9, minute=20)
        self.quote(11, 11.01)
        await self.engine._enter(self.engine._reentry_candidate(primary))
        self.assertIsNone(self.child())
        self.broker.validate_shortable.assert_not_called()

    async def test_reentry_fill_reported_at_deadline_is_covered_as_late_fill(self):
        primary = await self.closed_primary()
        child = await self.enter_reentry(primary)
        self.clock.value = self.clock.value.replace(hour=9, minute=20)
        self.broker.fill(child["entry_token"], 1000, 12.5)
        self.quote(12.49, 12.5)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["exit_reason"], "late_entry_fill")
        self.assertTrue(child["late_entry_fill"])
        self.assertEqual(child["cover_orders"][0]["qty"], 1000)

    async def test_configured_reentry_rules_can_extend_past_primary_exit(self):
        primary = await self.closed_primary()
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, reentry=ReentryConfig(enabled=True,
                entry_above_high_percent=7, stop_loss_percent=21, profit_target_percent=35,
                entry_deadline="09:40", time_exit="09:45")))
        self.clock.value = self.clock.value.replace(hour=9, minute=35)
        self.quote(10, 10.01)
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "entry_pending")
        self.assertEqual(child["entry_limit"], 10.7)
        self.assertEqual(child["stop_percent"], 21)
        self.assertEqual(child["target_percent"], 35)
        self.assertTrue(child["time_exit"].endswith("09:45:00-04:00"))

    async def test_actual_reentry_fill_controls_saved_stop_target_and_no_third_trade(self):
        primary = await self.closed_primary()
        child = await self.enter_reentry(primary)
        self.broker.fill(child["entry_token"], 1000, 12.5)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["stop_price"], 15)
        self.assertEqual(child["target_price"], 7.5)
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, reentry=ReentryConfig(enabled=False, stop_loss_percent=99)))
        self.quote(14.99, 15)
        await self.engine._manage_broker_trade(child)
        cover = child["cover_orders"][0]
        self.broker.fill(cover["token"], 1000, 15)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["exit_reason"], "stop_loss")
        self.assertEqual(child["status"], "closed")
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, reentry=ReentryConfig(enabled=True)))
        self.assertIsNone(self.engine._reentry_candidate(primary))
        self.assertIsNone(self.engine._reentry_candidate(child))
        await self.engine.tick()
        self.assertEqual(len([row for row in self.broker.submissions if row["side"] == "sell"]), 2)

    async def test_filled_reentry_exits_at_0920_using_saved_rule(self):
        primary = await self.closed_primary()
        child = await self.enter_reentry(primary)
        self.broker.fill(child["entry_token"], 1000, 12.5)
        await self.engine._manage_broker_trade(child)
        self.clock.value = self.clock.value.replace(hour=9, minute=20)
        self.quote(12, 12.01)
        await self.engine._manage_broker_trade(child)
        self.assertEqual(child["exit_reason"], "time_exit")
        self.assertEqual(child["cover_orders"][0]["qty"], 1000)

    async def test_legacy_closed_primary_uses_saved_candidate_high_and_single_child_survives_restart(self):
        primary = await self.closed_primary()
        primary.pop("trade_number")
        primary.pop("early_high")
        primary.pop("flat_confirmed_at")
        self.engine._persist()
        settings = self.engine.settings
        self.engine.store.close()
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(), feed=self.feed, now=self.clock)
        await self.engine.open()
        self.ready()
        self.quote(12.39, 12.4)
        primary = self.engine._day_state()["trades"]["TEST"]
        child = await self.enter_reentry(primary)
        self.assertEqual(child["entry_limit"], 10.5)
        token = child["entry_token"]
        self.engine.store.close()
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(), feed=self.feed, now=self.clock)
        self.engine._start = AsyncMock()  # No background loop; exercise persisted recovery only.
        await self.engine.open()
        self.engine._start.assert_awaited_once_with(allow_entries=False)
        self.ready()
        self.quote(12.39, 12.4)
        primary = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(self.child()["entry_token"], token)
        self.assertEqual(self.child()["stop_percent"], 20)
        self.assertIsNone(self.engine._reentry_candidate(primary))
        await self.engine.tick()
        self.assertEqual(len([row for row in self.broker.submissions if row["side"] == "sell"]), 2)
        self.assertEqual(len(self.broker.locates), 1)

    async def test_monitor_reentry_uses_current_bid_and_has_no_broker_calls(self):
        self.engine.settings = replace(self.engine.settings, mode="monitor")
        self.engine.broker = Mock()
        await self.engine._enter(self.candidate)
        primary = self.engine._day_state()["trades"]["TEST"]
        self.quote(12.4, 12.41)
        self.engine._manage_monitor_trade(primary)
        self.assertEqual(primary["exit_reason"], "stop_loss")
        child = await self.enter_reentry(primary)
        self.assertEqual(child["entry_avg_price"], 12.4)
        self.assertEqual(child["stop_price"], 14.88)
        self.assertEqual(child["target_price"], 7.44)
        self.assertFalse(self.engine.broker.mock_calls)

    async def test_monitor_resting_reentry_expires_without_fill_at_0920(self):
        self.engine.settings = replace(self.engine.settings, mode="monitor")
        self.engine.broker = Mock()
        await self.engine._enter(self.candidate)
        primary = self.engine._day_state()["trades"]["TEST"]
        self.quote(12.4, 12.41)
        self.engine._manage_monitor_trade(primary)
        self.quote(8, 8.01)
        child = await self.enter_reentry(primary)
        self.assertEqual(child["status"], "entry_pending")
        self.clock.value = self.clock.value.replace(hour=9, minute=20)
        self.quote(12.4, 12.41)
        self.engine._manage_monitor_trade(child)
        self.assertEqual(child["status"], "skipped")
        self.assertEqual(child["entry_filled_qty"], 0)
        self.assertFalse(self.engine.broker.mock_calls)


if __name__ == "__main__":
    unittest.main()
