"""Broker-safety integration checks; clocks, data, feed and broker are in memory."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from four_am_short.config import StrategyConfig
from four_am_short.models import Bar, EASTERN
from four_am_short.live.config import DasSettings, LiveSettings
from four_am_short.live.das import DasError, OrderRejected, OrderSubmissionUncertain, TERMINAL_STATUSES
from four_am_short.live.data import DiscoveryResult
from four_am_short.live.engine import LiveEngine


class FakeClock:
    def __init__(self):
        self.value = datetime(2026, 9, 21, 4, 25, tzinfo=EASTERN)
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeFeed:
    def __init__(self):
        self.symbols = set()
        self.running = False
    async def set_symbols(self, symbols):
        self.symbols = set(symbols)
    async def run(self, stop):
        self.running = True
        try:
            await stop.wait()
        finally:
            self.running = False


class FakeData:
    def __init__(self):
        self.closed = False
    async def discover(self, day):
        return DiscoveryResult(['TEST'], {'TEST': 7.0}, day - timedelta(days=3), True, [])
    async def backfill(self, symbols, day, start, end):
        return {'TEST': [Bar(datetime.combine(day, datetime.min.time(), EASTERN).replace(hour=4),
                             9.5, 10, 9, 9.8, 5000)]}
    async def close(self):
        self.closed = True


class FakeBroker:
    """An acknowledgment is deliberately not a fill or cancel acknowledgment."""
    def __init__(self, clock):
        self.clock = clock
        self.orders, self.rejections = {}, set()
        self.submissions, self.cancellations, self.locates = [], [], []
        self.locate_comparisons = {}
        self.counter = 100
        self.submit_failure = None
        self.cancel_failure = False
        self.locate_hook = self.before_send = None
        self.position_override = None
        self.closed = False
        self.service_count = 0
        self.lookup_failure = self.lookup_notify = self.cancel_hook = None
    def get_account(self):
        return {'id': 'SIMTEST', 'trading_blocked': False, 'buying_power': 100000}
    def new_client_order_id(self):
        self.counter += 1
        return str(self.counter)
    def position_qty(self, symbol):
        if self.position_override is not None:
            return self.position_override
        return sum(row['filled_qty'] * (1 if row['side'] == 'buy' else -1)
                   for row in self.orders.values() if row['symbol'] == symbol)
    def list_open_orders(self, symbol):
        return [dict(row) for row in self.orders.values()
                if row['symbol'] == symbol and row['status'] not in TERMINAL_STATUSES]
    def get_order_by_client_id(self, token):
        if self.lookup_notify:
            self.lookup_notify()
        if self.lookup_failure:
            raise self.lookup_failure
        if token in self.rejections:
            raise OrderRejected('Definitive complete unfilled rejection')
        row = self.orders.get(token)
        return dict(row) if row else None
    def get_order(self, order_id):
        return dict(next(row for row in self.orders.values() if row['id'] == order_id))
    def ensure_shortable(self, symbol, shares, max_price, *, still_valid=None):
        self.locates.append((symbol, shares, max_price))
        if self.locate_hook:
            self.locate_hook()
        return True, 'Simulated available borrow', 'existing', 0.0
    def validate_shortable(self, symbol, shares):
        return True, 'Available borrow'
    def service_locate_offers(self):
        self.service_count += 1
        return {'checked': True, 'rejected': 0}
    def submit_limit_order(self, *, symbol, qty, side, limit_price, client_order_id,
                           route=None, still_valid=None):
        request = dict(symbol=symbol, qty=qty, side=side, limit_price=limit_price,
                       client_order_id=client_order_id, route=route)
        if self.before_send:
            self.before_send(request)
        if still_valid is not None and not still_valid():
            self.rejections.add(client_order_id)
            raise OrderRejected('Eligibility ended before wire; no order sent')
        failure, self.submit_failure = self.submit_failure, None
        self.submissions.append(request)
        if failure == 'rejected':
            self.rejections.add(client_order_id)
            raise OrderRejected('Definitive complete unfilled rejection')
        row = dict(request, id=str(200 + len(self.orders)), status='new', filled_qty=0,
                   filled_avg_price=0.0, first_fill_time=None, last_fill_time=None)
        self.orders[client_order_id] = row
        if failure == 'uncertain':
            raise OrderSubmissionUncertain('No acknowledgment; token may already be accepted')
        return dict(row)
    def cancel_order(self, order_id):
        self.cancellations.append(order_id)
        if self.cancel_failure:
            self.cancel_failure = False
            raise DasError('Cancel send interrupted')
        if self.cancel_hook:
            self.cancel_hook(order_id)
    def fill(self, token, qty, average, *, status=None):
        row = self.orders[token]
        when = self.clock().isoformat()
        row.update(filled_qty=qty, filled_avg_price=average,
                   status=status or ('filled' if qty == row['qty'] else 'partially_filled'),
                   first_fill_time=row['first_fill_time'] or when, last_fill_time=when)
    def acknowledge_cancel(self, token):
        self.orders[token]['status'] = 'canceled'
    def close(self):
        self.closed = True


class EngineBrokerSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.broker, self.feed = FakeBroker(self.clock), FakeFeed()
        settings = LiveSettings(strategy=replace(StrategyConfig(), shares=1000), mode='das_paper',
                                state_dir=Path(self.directory.name), das=DasSettings(username='SIMUSER',
                                password='simulation-only', account='SIMTEST'), cover_replace_seconds=3)
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(), feed=self.feed, now=self.clock)
        await self.engine.open()
        self.engine.running = self.engine.entries_enabled = True
        self.engine._broker_status.update(connected=True, status='Verified test broker')
        self.engine._broker_check_at = self.clock() + timedelta(days=1)
        self.engine._market_day = self.engine._window_finalized = True
        self.engine._data_status.update(ready=True, connected=True, backfill_ready=True)
        self.engine._closes['TEST'] = 7.0
        self.engine._symbols = ['TEST']
        self.engine._accept_bar('TEST', Bar(datetime(2026, 9, 21, 4, 0, tzinfo=EASTERN), 9.5, 10, 9, 9.8, 5000), historical=True)
        self.candidate = self.engine._day_state()['candidates']['TEST']
        self.quote(9.20, 9.21)

    async def asyncTearDown(self):
        # Do not trigger trading lifecycle operations merely to clean test resources.
        tasks = [getattr(self.engine, name, None) for name in
                 ('_entry_task', '_bootstrap_task', '_finalize_task', '_locate_service_task', '_broker_health_task')]
        tasks += list(self.engine._tasks)
        for task in tasks:
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        self.engine.store.close()
        self.directory.cleanup()

    def quote(self, bid, ask):
        self.engine._quotes['TEST'] = dict(bid=bid, ask=ask, timestamp=self.clock().isoformat())
    def trade(self):
        return self.engine._day_state()['trades']['TEST']
    def covers(self):
        return [row for row in self.broker.submissions if row['side'] == 'buy']
    async def enter(self):
        await self.engine._enter(self.candidate)
        return self.trade()
    async def entered_position(self, average=9.5):
        trade = await self.enter()
        self.broker.fill(trade['entry_token'], 1000, average)
        await self.engine._manage_broker_trade(trade)
        return trade

    async def test_entry_ack_is_pending_until_confirmed_broker_fill(self):
        trade = await self.enter()
        self.assertEqual(trade['status'], 'entry_pending')
        self.assertEqual(trade['entry_filled_qty'], 0)
        self.assertIsNone(trade['entry_avg_price'])
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(self.broker.submissions[0]['limit_price'], 9)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['remaining_qty'], 0)
        self.assertFalse(self.covers())

    async def test_actual_execution_average_sets_stop_and_target(self):
        trade = await self.enter()
        self.broker.fill(trade['entry_token'], 400, 9.4)
        await self.engine._manage_broker_trade(trade)
        self.assertAlmostEqual(trade['stop_price'], 12.22)
        self.broker.fill(trade['entry_token'], 1000, 9.5)
        await self.engine._manage_broker_trade(trade)
        self.assertAlmostEqual(trade['entry_avg_price'], 9.5)
        self.assertAlmostEqual(trade['stop_price'], 12.35)
        self.assertAlmostEqual(trade['target_price'], 8.3125)
        self.quote(12.32, 12.34)
        await self.engine._manage_broker_trade(trade)
        self.assertFalse(self.covers())
        self.quote(12.34, 12.35)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['exit_reason'], 'stop_loss')
        self.assertEqual(self.covers()[0]['qty'], 1000)

    async def test_partial_entry_waits_for_cancellation_before_cover(self):
        trade = await self.enter()
        self.broker.fill(trade['entry_token'], 400, 9.5)
        self.quote(12.35, 12.36)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.broker.cancellations, [trade['entry_order_id']])
        self.assertFalse(self.covers())
        self.broker.fill(trade['entry_token'], 450, 9.55)
        await self.engine._manage_broker_trade(trade)
        self.assertFalse(self.covers())
        self.broker.acknowledge_cancel(trade['entry_token'])
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.covers()[0]['qty'], 450)
        self.assertEqual(trade['entry_filled_qty'], 450)

    async def test_partial_cover_replacement_uses_reconciled_remaining_quantity(self):
        trade = await self.entered_position()
        self.quote(8.29, 8.30)
        await self.engine._manage_broker_trade(trade)
        first = trade['cover_orders'][0]
        self.broker.fill(first['token'], 250, 8.30)
        self.clock.advance(4); self.quote(8.39, 8.40)
        await self.engine._manage_broker_trade(trade)
        self.assertIn(first['id'], self.broker.cancellations)
        self.assertEqual(len(self.covers()), 1)
        self.broker.fill(first['token'], 300, 8.31)
        self.broker.acknowledge_cancel(first['token'])
        await self.engine._manage_broker_trade(trade)
        self.assertEqual([row['qty'] for row in self.covers()], [1000, 700])
        second = trade['cover_orders'][1]
        self.broker.fill(second['token'], 700, 8.5)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['status'], 'closed')
        self.assertEqual(trade['remaining_qty'], 0)
        self.assertAlmostEqual(trade['exit_avg_price'], 8.443)
        self.assertAlmostEqual(trade['realized_pnl'], 1057)

    async def test_uncertain_entry_reconciles_without_second_submission(self):
        self.broker.submit_failure = 'uncertain'
        trade = await self.enter()
        self.assertEqual(trade['status'], 'uncertain')
        self.assertTrue(trade['reconciliation_required'])
        await self.engine._enter(self.candidate)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(len(self.broker.submissions), 1)
        self.broker.fill(trade['entry_token'], 1000, 9.6)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['status'], 'open')
        self.assertFalse(trade['reconciliation_required'])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_uncertain_cover_does_not_create_overlapping_buy(self):
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        self.broker.submit_failure = 'uncertain'
        with self.assertRaises(OrderSubmissionUncertain):
            await self.engine._manage_broker_trade(trade)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(len(self.covers()), 1)
        self.assertEqual(trade['remaining_qty'], 1000)

    async def test_deadline_after_locate_prevents_entry(self):
        self.broker.locate_hook = lambda: setattr(self.clock, 'value', self.clock().replace(hour=6, minute=0))
        trade = await self.enter()
        self.assertEqual(trade['status'], 'skipped')
        self.assertEqual(len(self.broker.locates), 1)
        self.assertFalse(self.broker.submissions)
        await self.engine._enter(self.candidate)
        self.assertEqual(len(self.broker.locates), 1)

    async def test_pause_during_stock_preflight_prevents_wire_submission(self):
        self.broker.before_send = lambda request: setattr(self.engine, 'entries_enabled', False)
        trade = await self.enter()
        self.assertEqual(trade['status'], 'skipped')
        self.assertFalse(self.broker.submissions)

    async def test_stop_entries_preserves_stop_supervision(self):
        trade = await self.entered_position()
        await self.engine.stop_entries()
        self.assertFalse(self.engine.entries_enabled)
        self.assertTrue(self.engine.running)
        self.assertFalse(self.covers())
        self.quote(12.4, 12.41)
        await self.engine.tick()
        self.assertEqual(trade['exit_reason'], 'stop_loss')
        self.assertEqual(self.covers()[0]['qty'], 1000)

    async def test_closed_stock_cannot_reenter_same_day(self):
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.broker.fill(trade['cover_orders'][0]['token'], 1000, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['status'], 'closed')
        await self.engine._enter(self.candidate)
        await self.engine.tick()
        self.assertEqual(len(self.broker.locates), 1)
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_entry_deadline_cancels_unfilled_order_and_skips_after_ack(self):
        trade = await self.enter()
        self.clock.value = self.clock().replace(hour=6, minute=0)
        self.quote(9.2, 9.21)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.broker.cancellations, [trade['entry_order_id']])
        self.broker.acknowledge_cancel(trade['entry_token'])
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['status'], 'skipped')
        self.assertFalse(self.covers())

    async def test_time_exit_uses_fresh_quote_and_confirmed_position(self):
        trade = await self.entered_position()
        self.clock.value = self.clock().replace(hour=9, minute=30)
        await self.engine._manage_broker_trade(trade)
        self.assertFalse(self.covers())
        self.assertEqual(trade['exit_reason'], 'time_exit')
        self.quote(10.0, 10.01)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(self.covers()[0]['qty'], 1000)

    async def test_foreign_position_blocks_locate_and_entry(self):
        self.broker.position_override = -25
        trade = await self.enter()
        self.assertEqual(trade['status'], 'skipped')
        self.assertFalse(self.broker.locates or self.broker.submissions)

    async def test_position_conflict_prevents_accidental_cover(self):
        trade = await self.entered_position()
        self.broker.position_override = -900
        self.quote(12.4, 12.41)
        with self.assertRaises(OrderSubmissionUncertain):
            await self.engine._manage_broker_trade(trade)
        self.assertFalse(self.covers())

    async def test_durable_engine_intent_precedes_stock_send(self):
        def inspect(request):
            state = json.loads(self.engine.settings.state_path.read_text())
            trade = state['days']['2026-09-21']['trades']['TEST']
            if request['side'] == 'sell':
                self.assertEqual(trade['entry_token'], request['client_order_id'])
            else:
                self.assertEqual(trade['cover_orders'][-1]['token'], request['client_order_id'])
        self.broker.before_send = inspect
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(len(self.broker.submissions), 2)

    async def test_confirmed_entry_rejection_falls_back_with_fresh_token(self):
        self.broker.submit_failure = 'rejected'
        trade = await self.enter()
        self.assertEqual([row['route'] for row in self.broker.submissions], ['ARCAE', 'CBATS'])
        self.assertNotEqual(self.broker.submissions[0]['client_order_id'], self.broker.submissions[1]['client_order_id'])
        self.assertEqual(trade['status'], 'entry_pending')

    async def test_late_definitive_entry_rejection_resolves_uncertain_token(self):
        self.broker.submit_failure = 'uncertain'
        trade = await self.enter()
        token = trade['entry_token']
        self.broker.orders.pop(token)
        self.broker.rejections.add(token)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['status'], 'skipped')
        self.assertTrue(trade['entry_terminal'])
        self.assertFalse(trade['reconciliation_required'])
        self.assertEqual(len(self.broker.submissions), 1)

    async def test_rejected_cover_uses_backup_after_confirming_position(self):
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        self.broker.submit_failure = 'rejected'
        await self.engine._manage_broker_trade(trade)
        self.clock.advance(4); self.quote(8.2, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual([row['route'] for row in self.covers()], ['ARCAE', 'CBATS'])
        self.assertEqual([row['qty'] for row in self.covers()], [1000, 1000])
        self.assertNotEqual(self.covers()[0]['client_order_id'], self.covers()[1]['client_order_id'])

    async def test_failed_cancel_send_is_retried_before_cover_replacement(self):
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.clock.advance(4); self.quote(8.4, 8.5)
        self.broker.cancel_failure = True
        with self.assertRaises(DasError):
            await self.engine._manage_broker_trade(trade)
        self.clock.advance(4); self.quote(8.4, 8.5)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(len(self.broker.cancellations), 2)
        self.assertEqual(len(self.covers()), 1)

    async def test_entry_time_uses_broker_execution_time_not_later_poll(self):
        trade = await self.enter()
        execution_at = self.clock().isoformat()
        self.broker.fill(trade['entry_token'], 1000, 9.5)
        self.clock.advance(60); self.quote(9.2, 9.21)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['entry_time'], execution_at)

    async def test_exit_time_uses_last_broker_cover_execution(self):
        trade = await self.entered_position()
        self.quote(8.2, 8.3)
        await self.engine._manage_broker_trade(trade)
        self.clock.advance(1)
        execution_at = self.clock().isoformat()
        self.broker.fill(trade['cover_orders'][0]['token'], 1000, 8.3)
        self.clock.advance(60)
        await self.engine._manage_broker_trade(trade)
        self.assertEqual(trade['exit_time'], execution_at)

    async def test_uncertain_entry_restart_resumes_supervision_with_entries_paused(self):
        self.broker.submit_failure = 'uncertain'
        trade = await self.enter()
        token = trade['entry_token']
        settings = self.engine.settings
        self.engine.store.close()
        self.engine._opened = self.engine.running = False
        self.feed = FakeFeed()
        self.engine = LiveEngine(settings, broker=self.broker, data=FakeData(), feed=self.feed, now=self.clock)
        looked_up = asyncio.Event()
        loop = asyncio.get_running_loop()
        self.broker.lookup_notify = lambda: loop.call_soon_threadsafe(looked_up.set)
        await self.engine.open()
        await asyncio.wait_for(looked_up.wait(), timeout=1)
        self.assertTrue(self.engine.running)
        self.assertFalse(self.engine.entries_enabled)
        self.assertEqual(self.trade()['entry_token'], token)
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertTrue(any(task.get_name() == '4am-supervisor' and not task.done()
                            for task in self.engine._tasks))
        self.assertTrue(any('Restored managed' in row['message'] for row in self.engine.state['events']))

    async def test_graceful_close_reconciles_entry_cancel_attempts_cover_and_preserves_exposure(self):
        trade = await self.enter()
        self.broker.fill(trade['entry_token'], 400, 9.5)
        await self.engine._manage_broker_trade(trade)
        self.engine.settings = replace(self.engine.settings, shutdown_grace_seconds=.04, poll_seconds=.005)
        def acknowledge(order_id):
            if order_id == trade['entry_order_id']:
                self.assertFalse(self.covers(), 'No cover may precede terminal entry confirmation')
                self.broker.acknowledge_cancel(trade['entry_token'])
        self.broker.cancel_hook = acknowledge
        with self.assertLogs('four_am_short.live.engine', level='CRITICAL') as logged:
            await self.engine.close()
        self.assertIn(trade['entry_order_id'], self.broker.cancellations)
        self.assertEqual([row['qty'] for row in self.covers()], [400])
        self.assertFalse(self.engine.running or self.engine._opened)
        self.assertTrue(self.broker.closed)
        self.assertIn('UNRESOLVED MANAGED EXPOSURE', '\n'.join(logged.output))
        saved = json.loads(self.engine.settings.state_path.read_text())['days']['2026-09-21']['trades']['TEST']
        self.assertTrue(saved['entry_terminal'])
        self.assertEqual(saved['remaining_qty'], 400)
        self.assertEqual(saved['status'], 'cover_pending')
        self.assertEqual(saved['exit_reason'], 'shutdown')
        self.assertEqual(len(saved['cover_orders']), 1)
        self.assertTrue(all(task.done() for task in self.engine._tasks))

    async def test_lookup_failure_keeps_position_active_and_blocks_new_entries(self):
        trade = await self.entered_position()
        self.broker.lookup_failure = DasError('Execution snapshot incomplete')
        second = dict(self.candidate, symbol='NEXT')
        self.engine._day_state()['candidates']['NEXT'] = second
        self.engine._symbols.append('NEXT')
        self.engine._closes['NEXT'] = 7.0
        self.engine._quotes['NEXT'] = dict(self.engine._quotes['TEST'])
        await self.engine.tick()
        self.assertIn(trade, self.engine._trades(active=True))
        self.assertTrue(trade['reconciliation_required'])
        self.assertEqual(trade['remaining_qty'], 1000)
        self.assertNotIn('NEXT', self.engine._day_state()['trades'])
        self.assertIn('verified DAS connection', second['note'])
        self.assertEqual(len(self.broker.submissions), 1)
        self.assertEqual(len(self.broker.locates), 1)


if __name__ == '__main__':
    unittest.main()
