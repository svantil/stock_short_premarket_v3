"""Offline price-band coverage across borrow, order, fill and cover lifecycles."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import Mock

from four_am_short.config import ReentryConfig
import test_4am_short_live_engine as broker_fixtures


class LiveEntryPriceRangeTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the in-memory clock/feed/broker fixtures, without inheriting all tests.
    asyncSetUp = broker_fixtures.EngineBrokerSafetyTests.asyncSetUp
    asyncTearDown = broker_fixtures.EngineBrokerSafetyTests.asyncTearDown
    quote = broker_fixtures.EngineBrokerSafetyTests.quote
    trade = broker_fixtures.EngineBrokerSafetyTests.trade
    covers = broker_fixtures.EngineBrokerSafetyTests.covers
    enter = broker_fixtures.EngineBrokerSafetyTests.enter
    entered_position = broker_fixtures.EngineBrokerSafetyTests.entered_position

    def configure_range(self):
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, min_entry_price=1, max_entry_price=10))

    async def test_entry_limit_outside_range_does_not_request_borrow(self):
        self.configure_range()
        for limit in (0.9999, 10.01):
            with self.subTest(limit=limit):
                self.candidate["entry_limit"] = limit
                self.quote(9.2, 9.21)
                self.assertFalse(self.engine._can_enter(self.candidate))
                await self.engine._enter(self.candidate)
                self.assertEqual(self.broker.locates, [])
                self.assertEqual(self.broker.submissions, [])
                self.assertEqual(self.engine._day_state()["trades"], {})

    async def test_live_bid_outside_range_does_not_request_borrow(self):
        self.configure_range()
        for bid in (0.9999, 10.01):
            with self.subTest(bid=bid):
                self.quote(bid, bid + 0.01)
                self.assertFalse(self.engine._can_enter(self.candidate))
                await self.engine._enter(self.candidate)
                self.assertEqual(self.broker.locates, [])
                self.assertEqual(self.broker.submissions, [])

    async def test_both_boundaries_are_inclusive(self):
        self.configure_range()
        for boundary in (1, 10):
            with self.subTest(boundary=boundary):
                self.candidate["entry_limit"] = boundary
                self.quote(boundary, boundary + 0.01)
                self.assertTrue(self.engine._can_enter(self.candidate))
                self.assertTrue(self.engine._can_locate(self.candidate))

    async def test_price_change_during_borrow_prevents_stock_order(self):
        self.configure_range()
        self.broker.locate_hook = lambda: self.quote(10.01, 10.02)
        trade = await self.enter()
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(trade["status"], "skipped")

    async def test_price_change_immediately_before_wire_prevents_order(self):
        self.configure_range()
        self.broker.before_send = lambda request: self.quote(10.01, 10.02)
        trade = await self.enter()
        self.assertEqual(self.broker.submissions, [])
        self.assertEqual(trade["status"], "skipped")
        self.assertEqual(trade["entry_filled_qty"], 0)

    async def test_pending_entry_cancels_when_bid_leaves_range(self):
        self.configure_range()
        trade = await self.enter()
        self.quote(10.01, 10.02)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.broker.cancellations, [trade["entry_order_id"]])
        self.assertTrue(trade["cancel_entry"])
        self.assertEqual(self.covers(), [])
        self.assertFalse(trade["entry_terminal"])

    async def test_quote_event_latches_brief_excursion_for_pending_order(self):
        self.configure_range()
        trade = await self.enter()
        await self.engine.on_event({"T": "q", "S": "TEST", "bp": 10.01, "ap": 10.02,
                                    "bs": 1, "as": 1, "t": self.clock().isoformat()})
        self.quote(9.2, 9.21)
        await self.engine._manage_broker_trade(trade)
        self.assertTrue(trade["cancel_entry"])
        self.assertEqual(self.broker.cancellations, [trade["entry_order_id"]])

    async def test_partial_fill_is_preserved_while_outside_range_remainder_cancels(self):
        self.configure_range()
        trade = await self.enter()
        self.broker.fill(trade["entry_token"], 200, 9.5)
        self.quote(10.01, 10.02)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade["entry_filled_qty"], 200)
        self.assertEqual(trade["remaining_qty"], 200)
        self.assertEqual(self.broker.cancellations, [trade["entry_order_id"]])
        self.assertEqual(self.covers(), [])
        self.broker.acknowledge_cancel(trade["entry_token"])
        self.quote(12.4, 12.41)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.covers()[-1]["qty"], 200)

    async def test_actual_outside_range_fill_remains_supervised(self):
        self.configure_range()
        trade = await self.enter()
        self.broker.fill(trade["entry_token"], 1000, 10.02)
        self.quote(10.02, 10.03)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade["entry_avg_price"], 10.02)
        self.assertEqual(trade["remaining_qty"], 1000)
        self.quote(13.5, 13.51)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.covers()[-1]["qty"], 1000)

    async def test_legacy_pending_order_outside_new_range_cancels_without_quote(self):
        self.candidate["entry_limit"] = 11
        self.quote(11, 11.01)
        trade = await self.enter()
        self.configure_range()
        self.engine._quotes.clear()
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.broker.cancellations, [trade["entry_order_id"]])
        self.assertFalse(trade["entry_terminal"])

    async def test_monitor_does_not_fill_pending_order_outside_range(self):
        self.configure_range()
        self.engine.settings = replace(self.engine.settings, mode="monitor")
        self.candidate["entry_limit"] = 10
        self.quote(9.9, 9.91)
        trade = await self.enter()
        self.assertEqual(trade["entry_filled_qty"], 0)
        self.quote(10.01, 10.02)
        self.engine._manage_monitor_trade(trade)
        self.assertEqual(trade["entry_filled_qty"], 0)
        self.assertEqual(trade["status"], "skipped")
        self.assertEqual(self.broker.submissions, [])

    async def test_reentry_above_range_does_not_validate_borrow_or_send_order(self):
        self.configure_range()
        self.engine.settings = replace(self.engine.settings, strategy=replace(
            self.engine.settings.strategy, reentry=ReentryConfig(enabled=True)))
        primary = await self.entered_position()
        self.quote(12.4, 12.41)
        await self.engine._manage_broker_trade(primary)
        cover = primary["cover_orders"][0]
        self.broker.fill(cover["token"], 1000, 12.41)
        await self.engine._manage_broker_trade(primary)
        self.assertEqual(primary["status"], "closed")
        self.quote(9.8, 9.81)
        candidate = self.engine._reentry_candidate(primary)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["entry_limit"], 10.5)
        self.broker.validate_shortable = Mock(return_value=(True, "Existing borrow"))
        await self.engine._enter(candidate)
        self.broker.validate_shortable.assert_not_called()
        self.assertNotIn("TEST:reentry", self.engine._day_state()["trades"])
        self.assertEqual(len(self.broker.submissions), 2)


if __name__ == "__main__":
    unittest.main()
