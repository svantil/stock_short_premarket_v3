"""Offline checks of live discovery recovery, timing and persisted ownership."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from four_am_short.config import load_config
from four_am_short.models import Bar, EASTERN
from four_am_short.live.config import DataSettings, LiveSettings, load_live_settings
from four_am_short.live.data import DiscoveryResult
from four_am_short.live.engine import LiveEngine, order_price
from four_am_short.live.store import StateStore


def instant(clock="04:30", day="2026-09-21"):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=EASTERN)


def bar(clock, high, close=None):
    return Bar(instant(clock), high, high, high, high if close is None else close, 100)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = instant()
        self.data = AsyncMock()
        self.data.discover.return_value = DiscoveryResult(
            symbols=["TEST"], previous_closes={"TEST": 10.0},
            previous_close_date=date(2026, 9, 18), market_day=True, warnings=[])
        self.data.backfill.return_value = {"TEST": [bar("04:00", 14)]}
        self.feed = AsyncMock()
        settings = LiveSettings(state_dir=Path(self.directory.name), poll_seconds=0.01,
                                shutdown_grace_seconds=0,
                                locate_trigger_below_entry_percent=None,
                                data=DataSettings(reconnect_seconds=3))
        self.engine = LiveEngine(settings, data=self.data, feed=self.feed, now=lambda: self.clock)
        await self.engine.open()
        self.engine._data_status["ready"] = True

    async def asyncTearDown(self):
        # These tests never start a supervisor or real client. Close store and
        # await any recovery tasks so no background worker escapes the test.
        self.engine.running = False
        for task in (self.engine._bootstrap_task, self.engine._finalize_task, self.engine._late_finalize_task, self.engine._entry_task):
            if task:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.engine.store.close()
        self.directory.cleanup()

    async def test_backfill_discovers_gap_that_has_already_faded(self):
        await self.engine._bootstrap()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["early_high"], 14)
        self.assertEqual(candidate["entry_limit"], 12.6)
        self.assertTrue(self.engine._window_finalized)
        self.feed.set_symbols.assert_awaited_with({"TEST"})

    async def test_complete_backfill_removes_unverified_persisted_candidates(self):
        self.engine._day_state()["candidates"]["OLD"] = {"symbol": "OLD", "early_high": 50}
        self.data.backfill.return_value = {}
        await self.engine._bootstrap()
        self.assertEqual(self.engine._day_state()["candidates"], {})
        self.assertTrue(self.engine._window_finalized)

    async def test_transient_discovery_failure_retries_without_reconnecting(self):
        self.data.discover.side_effect = [RuntimeError("temporary unavailable"), self.data.discover.return_value]
        await self.engine._bootstrap()
        self.assertFalse(self.engine._data_status["backfill_ready"])
        self.clock += timedelta(seconds=6)
        self.engine.running = True
        await self.engine.tick()
        await self.engine._bootstrap_task
        self.assertTrue(self.engine._data_status["backfill_ready"])
        self.assertEqual(self.data.discover.await_count, 2)

    async def test_final_window_failure_retries_after_backoff(self):
        await self.engine._bootstrap()
        self.engine._window_finalized = False
        self.data.backfill.side_effect = [RuntimeError("temporary"), {"TEST": [bar("04:14", 15)]}]
        await self.engine._finalize_window()
        self.assertFalse(self.engine._window_finalized)
        await self.engine.tick()
        self.assertIsNone(self.engine._finalize_task)
        self.clock += timedelta(seconds=6)
        await self.engine.tick()
        await self.engine._finalize_task
        self.assertEqual(self.engine._day_state()["candidates"]["TEST"]["early_high"], 15)

    async def test_yesterday_finalizer_cannot_finalize_todays_window(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            started.set()
            await release.wait()
            return {"TEST": [bar("04:14", 15)]}
        self.data.backfill.side_effect = delayed
        old = asyncio.create_task(self.engine._finalize_window())
        await started.wait()
        self.engine._day += timedelta(days=1)
        release.set()
        await old
        self.assertFalse(self.engine._window_finalized)
        self.assertEqual(self.engine._day_state()["candidates"], {})

    async def test_rollover_cancels_yesterdays_bootstrap_and_launches_today(self):
        self.engine._bootstrap_task = asyncio.create_task(asyncio.sleep(999))
        old = self.engine._bootstrap_task
        self.clock = instant("03:00", "2026-09-22")
        self.engine.running = True
        await self.engine.tick()
        await asyncio.gather(old, return_exceptions=True)
        self.assertTrue(old.cancelled())
        await self.engine._bootstrap_task
        self.data.discover.assert_awaited_with(date(2026, 9, 22))

    async def test_strict_gap_complete_window_last_high_and_delay(self):
        self.engine._closes = {"TEST": 10}
        self.engine._symbols = ["TEST"]
        self.engine._accept_bar("TEST", bar("04:00", 13))
        self.assertNotIn("TEST", self.engine._day_state()["candidates"])
        self.engine._accept_bar("TEST", bar("04:05", 14))
        self.engine._accept_bar("TEST", bar("04:14", 14))
        self.engine._accept_bar("TEST", bar("04:15", 100))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["early_high"], 14)
        self.assertEqual(candidate["early_high_time"], instant("04:15").isoformat())
        self.assertEqual(candidate["active_at"], instant("04:25").isoformat())
        self.assertFalse(self.engine._can_enter(candidate))
        self.engine.running = self.engine.entries_enabled = True
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status["backfill_ready"] = True
        await self.quote(12.7, 12.8)
        self.assertTrue(self.engine._can_enter(candidate))
        self.engine._closes = {}
        self.assertFalse(self.engine._can_enter(candidate))

    async def quote(self, bid, ask, when=None):
        await self.engine.on_event({"T": "q", "S": "TEST", "bp": bid, "ap": ask,
                                    "bs": 1, "as": 1, "t": (when or self.clock).isoformat()})

    async def test_monitor_places_no_broker_calls_and_uses_bid_ask(self):
        broker = AsyncMock()
        self.engine.broker = broker
        await self.engine._bootstrap()
        self.engine.running = self.engine.entries_enabled = True
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.quote(12.8, 12.9)
        await self.engine._enter(candidate)
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["entry_avg_price"], 12.8)
        self.assertEqual(trade["stop_price"], 16.64)
        self.assertEqual(trade["target_price"], 11.2)
        await self.quote(11.1, 11.2)
        self.engine._manage_monitor_trade(trade)
        self.assertEqual(trade["exit_reason"], "profit_target")
        self.assertEqual(trade["exit_avg_price"], 11.2)
        self.assertAlmostEqual(trade["realized_pnl"], 1600)
        await self.engine._enter(candidate)
        self.assertEqual(len(self.engine._trades()), 1)
        self.assertFalse(broker.mock_calls)

    async def test_quote_validation_rejects_future_crossed_and_stale_for_entry(self):
        await self.engine._bootstrap()
        self.engine.running = self.engine.entries_enabled = True
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.quote(13, 12)
        self.assertIsNone(self.engine._fresh_quote("TEST"))
        await self.quote(12, 13, self.clock + timedelta(seconds=10))
        self.assertIsNone(self.engine._fresh_quote("TEST"))
        await self.quote(12, 13, self.clock - timedelta(seconds=6))
        self.assertFalse(self.engine._can_enter(candidate))
        await self.quote(12, 13)
        self.assertTrue(self.engine._can_enter(candidate))

    async def test_stop_touch_is_latched_between_supervisor_polls(self):
        await self.engine._bootstrap()
        self.engine.running = self.engine.entries_enabled = True
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.quote(13, 13.1)
        await self.engine._enter(candidate)
        trade = self.engine._trades()[0]
        await self.quote(16.8, 16.9)
        await self.quote(13, 13.1)
        self.engine._manage_monitor_trade(trade)
        self.assertEqual(trade["exit_reason"], "stop_loss")

    async def test_pause_does_not_disable_existing_position_target(self):
        await self.engine._bootstrap()
        self.engine.running = self.engine.entries_enabled = True
        await self.quote(13, 13.1)
        await self.engine._enter(self.engine._day_state()["candidates"]["TEST"])
        await self.engine.stop_entries()
        await self.quote(11, 11.1)
        await self.engine.tick()
        self.assertEqual(self.engine._trades()[0]["status"], "closed")

    async def test_exact_deadline_does_not_fill_monitor_limit(self):
        await self.engine._bootstrap()
        self.engine.running = self.engine.entries_enabled = True
        await self.quote(12, 12.1)
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.engine._enter(candidate)
        self.clock = instant("06:00")
        await self.quote(13, 13.1)
        await self.engine.tick()
        trade = self.engine._trades()[0]
        self.assertEqual(trade["status"], "skipped")
        self.assertEqual(trade["entry_filled_qty"], 0)

    def enable_late_gaps(self, clock="07:30"):
        self.clock = instant(clock)
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, late_gap_enabled=True, entry_deadline="09:00"))
        self.engine._closes = {"TEST": 10}
        self.engine._symbols = ["TEST"]
        self.engine.running = self.engine.entries_enabled = True
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status.update(ready=True, backfill_ready=True)

    async def test_later_gap_uses_high_delay_after_early_window_finalizes(self):
        self.enable_late_gaps("07:10")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertTrue(candidate["late_gap"])
        self.assertEqual(candidate["active_at"], instant("07:15").isoformat())
        self.assertEqual(candidate["window_end"], instant("07:15").isoformat())
        await self.quote(12.8, 12.9)
        self.assertFalse(self.engine._can_enter(candidate))
        self.clock = instant("07:15:34")
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14)]}
        await self.engine._finalize_late_windows()
        self.data.backfill.assert_not_awaited()
        self.assertFalse(self.engine._can_enter(candidate))
        self.clock = instant("07:15:35")
        await self.engine._finalize_late_windows()
        self.data.backfill.assert_awaited_with(["TEST"], date(2026, 9, 21), "07:00", "07:15")
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.quote(12.8, 12.9)
        self.assertTrue(self.engine._can_enter(candidate))
        await self.engine._enter(candidate)
        trade = self.engine._day_state()["trades"]["TEST"]
        self.assertEqual(trade["entry_limit"], 12.6)
        self.assertEqual(trade["entry_avg_price"], 12.8)
        self.assertEqual(trade["stop_price"], 16.64)
        self.assertEqual(trade["target_price"], 11.2)
        self.engine._accept_bar("TEST", bar("07:14", 25))
        self.assertEqual(trade["early_high"], 14)
        self.assertEqual(trade["entry_limit"], 12.6)
        self.assertEqual(self.engine._day_state()["candidates"]["TEST"]["early_high"], 14)

    async def test_later_fresh_and_repeated_highs_set_delay_within_fixed_window(self):
        self.enable_late_gaps()
        for clock, high in (("07:00", 14), ("07:08", 15), ("07:12", 15)):
            self.engine._accept_bar("TEST", bar(clock, high))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["early_high"], 15)
        self.assertEqual(candidate["active_at"], instant("07:23").isoformat())
        self.assertEqual(candidate["window_end"], instant("07:15").isoformat())
        self.engine._accept_bar("TEST", bar("07:23", 25))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["early_high"], 15)
        self.assertEqual(candidate["entry_limit"], 13.5)
        self.assertEqual(candidate["active_at"], instant("07:23").isoformat())

    async def test_later_gap_cannot_enter_using_replaced_candidate(self):
        self.enable_late_gaps("07:15")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        stale = self.engine._day_state()["candidates"]["TEST"]
        self.engine._accept_bar("TEST", bar("07:08", 15))
        await self.quote(14, 14.1)
        self.assertFalse(self.engine._can_enter(stale))
        await self.engine._enter(stale)
        self.assertEqual(self.engine._day_state()["trades"], {})

    async def test_later_scanning_keeps_original_early_setup(self):
        self.enable_late_gaps()
        self.engine._accept_bar("TEST", bar("04:05", 14), historical=True)
        self.engine._accept_bar("TEST", bar("07:00", 25))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertFalse(candidate["late_gap"])
        self.assertEqual(candidate["early_high"], 14)
        self.assertEqual(candidate["active_at"], instant("04:16").isoformat())

    async def test_later_backfill_finds_faded_gap_and_replaces_stale_history(self):
        self.enable_late_gaps()
        self.engine._accept_bar("TEST", bar("05:00", 40))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14), bar("07:20", 11)]}
        await self.engine._bootstrap()
        self.data.backfill.assert_awaited_with(["TEST"], date(2026, 9, 21), "04:00", "07:30")
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["first_gap_time"], instant("07:00").isoformat())
        self.assertEqual(candidate["early_high"], 14)
        self.assertTrue(self.engine._window_finalized)
        self.assertTrue(candidate["window_finalized"])

    async def test_later_backfill_preserves_new_websocket_minutes(self):
        self.enable_late_gaps()
        async def delayed(*args):
            self.clock = instant("07:31")
            self.engine._accept_bar("TEST", bar("07:30", 14))
            return {}
        self.data.backfill.side_effect = delayed
        await self.engine._bootstrap()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["first_gap_time"], instant("07:30").isoformat())

    async def test_early_finalizer_preserves_later_candidates(self):
        self.enable_late_gaps()
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.return_value = {}
        await self.engine._finalize_window()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["first_gap_time"], instant("07:00").isoformat())
        self.assertEqual(candidate["active_at"], instant("07:15").isoformat())

    async def test_later_gap_does_not_extend_exclusive_entry_deadline(self):
        self.enable_late_gaps("09:01")
        self.engine._accept_bar("TEST", bar("09:00", 14))
        self.assertNotIn("TEST", self.engine._day_state()["candidates"])
        self.engine._accept_bar("TEST", bar("08:55", 14))
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["active_at"], instant("09:10").isoformat())
        await self.quote(13, 13.1)
        self.assertFalse(self.engine._can_enter(candidate))
        await self.engine.tick()
        self.assertEqual(candidate["status"], "expired")

    async def test_later_window_rest_correction_sets_high_before_freezing(self):
        self.enable_late_gaps("07:16")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14), bar("07:14", 20)]}
        await self.engine._finalize_late_windows()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertTrue(candidate["window_finalized"])
        self.assertEqual(candidate["early_high"], 20)
        self.assertEqual(candidate["active_at"], instant("07:25").isoformat())
        self.engine._accept_bar("TEST", bar("07:14", 25))
        self.assertEqual(candidate["early_high"], 20)
        self.data.backfill.return_value = {}
        await self.engine._finalize_window()
        self.assertFalse(self.engine._day_state()["candidates"]["TEST"]["window_finalized"])
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14), bar("07:14", 20)]}
        await self.engine._finalize_late_windows()
        self.assertTrue(self.engine._day_state()["candidates"]["TEST"]["window_finalized"])
        self.assertEqual(self.engine._day_state()["candidates"]["TEST"]["early_high"], 20)

    async def test_later_window_correction_restarts_window_if_first_gap_moves(self):
        self.enable_late_gaps("07:16")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 12), bar("07:05", 15)]}
        await self.engine._finalize_late_windows()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["first_gap_time"], instant("07:05").isoformat())
        self.assertEqual(candidate["window_end"], instant("07:20").isoformat())
        self.assertFalse(candidate["window_finalized"])

    async def test_later_window_failure_retries_without_allowing_entry(self):
        self.enable_late_gaps("07:16")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.side_effect = [RuntimeError("temporary"), {"TEST": [bar("07:00", 14)]}]
        await self.engine._finalize_late_windows()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        await self.quote(13, 13.1)
        self.assertFalse(self.engine._can_enter(candidate))
        await self.engine.tick()
        self.assertIsNone(self.engine._late_finalize_task)
        self.clock += timedelta(seconds=6)
        await self.engine.tick()
        await self.engine._late_finalize_task
        self.assertTrue(self.engine._day_state()["candidates"]["TEST"]["window_finalized"])

    async def test_bootstrap_request_before_correction_delay_cannot_finalize_late_window(self):
        self.enable_late_gaps("07:15")
        async def delayed(*args):
            self.clock = instant("07:15:40")
            return {"TEST": [bar("07:00", 14)]}
        self.data.backfill.side_effect = delayed
        await self.engine._bootstrap()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertFalse(candidate["window_finalized"])
        await self.quote(13, 13.1)
        self.assertFalse(self.engine._can_enter(candidate))

    async def test_later_window_shift_to_earlier_gap_needs_full_rest_coverage(self):
        self.enable_late_gaps("07:16")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        async def delayed(*args):
            self.engine._accept_bar("TEST", bar("06:55", 15))
            return {"TEST": [bar("07:00", 14)]}
        self.data.backfill.side_effect = delayed
        await self.engine._finalize_late_windows()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertEqual(candidate["first_gap_time"], instant("06:55").isoformat())
        self.assertEqual(candidate["window_end"], instant("07:10").isoformat())
        self.assertFalse(candidate["window_finalized"])

    async def test_stale_finalized_late_candidate_cannot_bypass_current_readiness(self):
        self.enable_late_gaps("07:16")
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14)]}
        await self.engine._bootstrap()
        stale = self.engine._day_state()["candidates"]["TEST"]
        self.assertTrue(stale["window_finalized"])
        self.engine._day_state()["candidates"]["TEST"] = {**stale, "window_finalized": False}
        await self.quote(13, 13.1)
        self.assertFalse(self.engine._can_enter(stale))

    async def test_two_later_stocks_have_independent_windows_and_frozen_trades(self):
        self.enable_late_gaps("07:16")
        self.engine._symbols.append("NEXT")
        self.engine._closes["NEXT"] = 10
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.engine._accept_bar("NEXT", bar("07:10", 15))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14)]}
        await self.engine._finalize_late_windows()
        self.data.backfill.assert_awaited_once_with(["TEST"], date(2026, 9, 21), "07:00", "07:15")
        first, second = (self.engine._day_state()["candidates"][symbol] for symbol in ("TEST", "NEXT"))
        self.assertTrue(first["window_finalized"])
        self.assertFalse(second["window_finalized"])
        self.assertEqual(second["window_end"], instant("07:25").isoformat())
        await self.quote(12.8, 12.9)
        await self.engine.on_event({"T": "q", "S": "NEXT", "bp": 14, "ap": 14.1,
                                    "bs": 1, "as": 1, "t": self.clock.isoformat()})
        self.assertTrue(self.engine._can_enter(first))
        self.assertFalse(self.engine._can_enter(second))
        await self.engine._enter(first)
        await self.engine._enter(second)
        self.assertEqual(set(self.engine._day_state()["trades"]), {"TEST"})
        self.engine._accept_bar("TEST", bar("07:14", 25))
        self.engine._accept_bar("NEXT", bar("07:15", 16))
        self.assertEqual(self.engine._day_state()["trades"]["TEST"]["early_high"], 14)
        self.assertEqual(self.engine._day_state()["candidates"]["NEXT"]["early_high"], 16)

    async def test_later_entry_waits_for_authoritative_original_window(self):
        self.enable_late_gaps("07:16")
        self.engine._window_finalized = False
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14)]}
        await self.engine._finalize_late_windows()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertTrue(candidate["window_finalized"])
        await self.quote(13, 13.1)
        self.assertFalse(self.engine._can_enter(candidate))
        await self.engine._enter(candidate)
        self.assertEqual(self.engine._day_state()["trades"], {})

    async def test_recovered_early_qualifier_reclassifies_unconsumed_late_setup(self):
        self.enable_late_gaps("07:16")
        self.engine._accept_bar("TEST", bar("07:00", 14))
        self.data.backfill.return_value = {"TEST": [bar("07:00", 14)]}
        await self.engine._finalize_late_windows()
        late = self.engine._day_state()["candidates"]["TEST"]
        self.assertTrue(late["window_finalized"])
        self.engine._window_finalized = False
        self.data.backfill.return_value = {"TEST": [bar("04:14", 20)]}
        await self.engine._finalize_window()
        candidate = self.engine._day_state()["candidates"]["TEST"]
        self.assertFalse(candidate["late_gap"])
        self.assertEqual(candidate["first_gap_time"], instant("04:14").isoformat())
        self.assertEqual(candidate["early_high"], 20)
        self.assertEqual(candidate["active_at"], instant("04:25").isoformat())
        await self.quote(19, 19.1)
        self.assertTrue(self.engine._can_enter(candidate))
        self.assertFalse(self.engine._can_enter(late))


class ConfigurationAndStoreTests(unittest.TestCase):
    def test_default_live_config_has_no_automatic_order_permission(self):
        # User-editable execution mode must not make the regression suite fail.
        # Loading a missing mode still defaults to monitor independently of it.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "live.json"
            path.write_text(json.dumps({"strategy_config": str(LiveSettings().strategy_config_path)}))
            settings = load_live_settings(path)
        self.assertEqual(settings.mode, "monitor")
        self.assertFalse(settings.execution_enabled)
        self.assertEqual(settings.strategy, load_config(settings.strategy_config_path).strategy)
        self.assertEqual(settings.data.paper, False)

    def test_secrets_do_not_appear_in_public_rules_or_settings_repr(self):
        settings = replace(LiveSettings(), data=DataSettings(api_key="secret-key", secret_key="secret-value"))
        public = json.dumps(settings.public_rules()) + repr(settings)
        self.assertNotIn("secret-key", public)
        self.assertNotIn("secret-value", public)

    def test_account_mode_identity_and_atomic_state_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            first = StateStore(path, "account1")
            state = first.open()
            state["days"]["2026-09-21"] = {"trades": {}, "candidates": {}}
            first.save(state)
            second = StateStore(path, "account1")
            with self.assertRaisesRegex(RuntimeError, "Another"):
                second.open()
            first.close()
            self.assertEqual(second.open(), state)
            second.close()
            with self.assertRaisesRegex(RuntimeError, "another execution account"):
                StateStore(path, "account2").open()

    def test_corrupt_state_is_not_replaced_with_an_empty_book(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("broken")
            with self.assertRaises(RuntimeError):
                StateStore(path, "account1").open()
            self.assertEqual(path.read_text(), "broken")

    def test_price_rounding_does_not_short_below_requested_limit(self):
        self.assertEqual(order_price(12.605), 12.61)
        self.assertEqual(order_price(.90001), .9001)


if __name__ == "__main__":
    unittest.main()
