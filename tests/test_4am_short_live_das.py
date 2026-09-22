"""DAS protocol checks use an in-memory socket, never a broker endpoint."""
import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from four_am_short.live.config import DasSettings
from four_am_short.live.das import DasClient, DasError, OrderRejected, OrderSubmissionUncertain


def order_line(oid='20', token='456', side='SS', qty=100, leaves=100, canceled=0,
               status='Accepted', account='TESTACCOUNT', price='5.25', route='ARCAE'):
    return (f'%ORDER {oid} {token} TEST {side} L {qty} {leaves} {canceled} {price} '
            f'{route} {status} 04:25:00 0 {account} TESTUSER CMDAPI DAY+ N/A')


class Wire:
    def __init__(self):
        self.commands, self.buffer = [], b''
        self.positions, self.orders, self.trades, self.locates = [], [], [], {}
        self.no_ack = self.partial = self.reject = self.partial_reject = False
        self.conflicting_reject = self.omit_end = self.login_failed = False
        self.only_login_ends = self.closed = self.purchase_no_ack = False
        self.short_info, self.available = '$SHORTINFO TEST N 0 Y 0 0 N N', 0
        self.prices = {'LOCATE4': '0.03', 'LOCATE6': '0.02', 'LOCATE10': '0.01'}
        self.minimums = dict.fromkeys(self.prices, '0')
        self.quote_available, self.sent_callback, self.locate_mutator = 1000, None, None
    def settimeout(self, timeout): pass
    def close(self): self.closed, self.buffer = True, b''
    def queue(self, *lines): self.buffer += ('\r\n'.join(lines) + '\r\n').encode('ascii')
    def locate_line(self, row):
        return (f"%SLOrder {row['id']} TEST {row['qty']} {row['open']} {row['filled']} "
                f"{row['price']} {row['status']} {row['route']} 04:25:00 0 {row['token']}")
    def block(self, name):
        begin, end = {'POSITIONS': ('#POS symb type qty', '#POSEND'),
                      'ORDERS': ('#Order id token symb', '#OrderEnd'),
                      'TRADES': ('#Trade id symb', '#TradeEnd'),
                      'LOCATES': ('#SLOrder id symb', '#SLOrderEnd')}[name]
        rows = {'POSITIONS': self.positions, 'ORDERS': self.orders, 'TRADES': self.trades,
                'LOCATES': [self.locate_line(row) for row in self.locates.values()]}[name]
        if not self.only_login_ends: self.queue(begin, *rows)
        if not self.omit_end: self.queue(end)
    def sendall(self, data):
        command = data.decode().strip()
        self.commands.append(command)
        if self.sent_callback: self.sent_callback(command)
        p = command.split()
        if p[0] == 'LOGIN':
            if self.login_failed: self.queue('#OrderServer:Logon:Failed')
            else:
                for name in ['POSITIONS', 'ORDERS', 'TRADES']: self.block(name)
        elif command == 'GET BP': self.queue('BP 25000 20000')
        elif p[:2] == ['GET', 'SHORTINFO']: self.queue(self.short_info)
        elif p[0] == 'GET': self.block(p[1])
        elif p[0] == 'SLAvailQuery': self.queue(f'$SLAvailQueryRet TESTACCOUNT TEST {self.available}')
        elif p[0] == 'SLRouteMinCharge': self.queue(f'SLRouteMinChargeRet {p[1]} {self.minimums[p[1]]}')
        elif p[0] == 'SLPRICEINQUIRE':
            self.queue(f'%SLRET 1 TEST {self.prices[p[3]]} {self.quote_available} {p[3]} TESTACCOUNT')
        elif p[0] == 'SLNEWORDER':
            qty, route = int(p[2]), p[3]
            paid = route == 'LOCATE10'
            row = dict(id=str(91 + len(self.locates)), qty=qty, route=route, token=p[4],
                       price=self.prices[route], status='Located' if paid else 'Offered',
                       open=0 if paid else qty, filled=qty if paid else 0)
            if self.locate_mutator: self.locate_mutator(row, paid)
            self.locates[row['id']] = row
            if paid: self.available += qty
            if not (paid and self.purchase_no_ack): self.queue(self.locate_line(row))
        elif p[0] == 'SLOFFEROPERATION':
            row = self.locates[p[1]]
            if p[2] == 'Accept':
                row.update(status='Located', filled=row['qty'], open=0)
                self.available += row['qty']
                if self.purchase_no_ack: return
            else: row.update(status='Closed', filled=0, open=0)
            self.queue(self.locate_line(row))
        elif p[0] == 'NEWORDER':
            qty, side, token, route, price = int(p[5]), p[2], p[1], p[4], p[6]
            if self.reject or self.partial_reject or self.conflicting_reject:
                rejected_qty = qty - 1 if self.partial_reject else qty
                self.queue(f'%OrderAct 0 Send_Rej {side} TEST {rejected_qty} {price} {route} 04:25:00 Cannot route {token}')
                if not self.conflicting_reject: return
            filled, oid = 40 if self.partial else 0, str(20 + len(self.orders))
            row = order_line(oid, token, side, qty, qty-filled, status='Partial' if filled else 'Accepted', price=price, route=route)
            self.orders.append(row)
            if filled: self.trades.append(f'%TRADE 9 TEST {side} 40 5.26 {route} 04:25:00 {oid} A 0 0')
            if not self.no_ack: self.queue(row)
    def recv(self, size):
        if not self.buffer: raise socket.timeout()
        data, self.buffer = self.buffer[:37], self.buffer[37:]
        return data


class DasTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path, self.wire = Path(self.directory.name)/'4am_short_das.json', Wire()
        self.settings = DasSettings(username='TESTUSER', password='test-only-password', account='TESTACCOUNT',
                                    timeout_seconds=.1, locate_quote_wait_seconds=.02)
        self.client = self.make_client()
    def make_client(self, **changes):
        if hasattr(self, 'client'): self.client.close()
        client = DasClient(replace(self.settings, **changes), journal_path=self.path,
                           socket_factory=lambda *_a, **_kw: self.wire)
        self.addCleanup(client.close)
        return client
    def submit(self, **changes):
        return self.client.submit_limit_order(**(dict(symbol='TEST', qty=100, side='sell', limit_price=5.25,
                                                      client_order_id='456') | changes))
    def stock_commands(self): return [c for c in self.wire.commands if c.startswith('NEWORDER')]
    def paid_commands(self):
        return [c for c in self.wire.commands if (c.startswith('SLNEWORDER') and ' LOCATE10 ' in c) or c.endswith(' Accept')]
    def test_lazy_readonly_login_and_complete_snapshots(self):
        self.assertEqual(self.wire.commands, [])
        self.assertEqual(self.client.get_account()['buying_power'], 25000)
        self.assertFalse(self.stock_commands() or self.paid_commands())
    def test_login_requires_beginnings_and_endings(self):
        self.wire.only_login_ends = True
        with self.assertRaises(DasError): self.client.get_account()
    def test_login_failure(self):
        self.wire.login_failed = True
        with self.assertRaisesRegex(DasError, 'not authenticated'): self.client.get_account()
    def test_position_short_sign_and_incomplete_snapshot(self):
        self.wire.positions = ['%POS TEST 3 80 5.20 0 0 0 2026/01/02-04:25:00 0']
        self.assertEqual(self.client.position_qty('TEST'), -80)
        self.wire.positions = []
        self.assertEqual(self.client.position_qty('TEST'), 0)
        self.wire.omit_end = True
        with self.assertRaises(DasError): self.client.position_qty('TEST')
    def test_multiple_position_types_block(self):
        self.wire.positions = ['%POS TEST 3 80 5 0 0 0 time 0', '%POS TEST 2 20 5 0 0 0 time 0']
        with self.assertRaisesRegex(DasError, 'Multiple'): self.client.position_qty('TEST')
    def test_account_order_filter(self):
        self.wire.orders = [order_line(account='OTHER'), order_line(oid='21', token='457')]
        self.assertEqual([r['id'] for r in self.client.list_open_orders('TEST')], ['21'])
    def test_short_and_cover_limit_orders_day_plus(self):
        self.submit()
        self.submit(side='buy', limit_price=4.055, client_order_id='457')
        self.assertEqual(self.stock_commands(), ['NEWORDER 456 SS TEST ARCAE 100 5.25 TIF=DAY+',
                                                'NEWORDER 457 B TEST ARCAE 100 4.06 TIF=DAY+'])
    def test_token_intent_durable_before_send(self):
        def inspect(command):
            if command.startswith('NEWORDER'):
                self.assertIn('456', json.loads(self.path.read_text())['orders'])
                self.assertNotIn('test-only-password', self.path.read_text())
        self.wire.sent_callback = inspect
        self.submit()
    def test_partial_fill_uses_execution_price(self):
        self.wire.partial = True
        result = self.submit()
        self.assertEqual((result['filled_qty'], result['status']), (40, 'partially_filled'))
        self.assertAlmostEqual(result['filled_avg_price'], 5.26)
        self.assertIn('T04:25:00', result['first_fill_time'])
        self.assertEqual(result['first_fill_time'], result['last_fill_time'])
    def test_entry_paused_during_preflight_never_sends_stock_order(self):
        with self.assertRaisesRegex(OrderRejected, 'no stock order sent'):
            self.submit(still_valid=lambda: False)
        self.assertFalse(self.stock_commands())
        self.client = self.make_client()
        with self.assertRaisesRegex(OrderRejected, 'no stock order sent'):
            self.client.get_order_by_client_id('456')
    def test_fill_time_uses_owned_eastern_submission_date_after_restart(self):
        self.wire.partial = True
        self.submit()
        self.client.close()
        journal = json.loads(self.path.read_text())
        journal['orders']['456']['submitted_at'] = '2026-01-02T04:24:59-05:00'
        self.path.write_text(json.dumps(journal))
        self.client = self.make_client()
        result = self.client.get_order('20')
        self.assertEqual(result['first_fill_time'], '2026-01-02T04:25:00-05:00')
    def test_partial_cancellation_retains_fills(self):
        self.wire.orders = [order_line(leaves=0, canceled=60, status='Canceled')]
        self.wire.trades = ['%TRADE 9 TEST SS 40 5.26 ARCAE 04:25:00 20 A 0 0']
        result = self.client.get_order('20')
        self.assertEqual((result['filled_qty'], result['status']), (40, 'canceled'))
    def test_missing_execution_reports_block(self):
        self.wire.orders = [order_line(leaves=0, status='Executed')]
        with self.assertRaises(DasError): self.client.get_order('20')
    def test_previously_observed_execution_cannot_disappear_after_restart(self):
        self.wire.partial = True
        self.submit()
        self.wire.orders = [order_line(leaves=0, canceled=100, status='Canceled')]
        self.wire.trades = []
        self.client = self.make_client()
        with self.assertRaisesRegex(OrderSubmissionUncertain, 'executions missing'):
            self.client.get_order('20')
    def test_uncertain_submission_restart_never_resends(self):
        self.wire.no_ack = True
        with self.assertRaises(OrderSubmissionUncertain): self.submit()
        self.client = self.make_client()
        self.assertEqual(self.client.get_order_by_client_id('456')['id'], '20')
        with self.assertRaises(OrderSubmissionUncertain): self.submit()
        self.assertEqual(len(self.stock_commands()), 1)
    def test_owned_missing_token_remains_uncertain(self):
        self.submit()
        self.wire.orders = []
        self.client = self.make_client()
        with self.assertRaises(OrderSubmissionUncertain): self.client.get_order_by_client_id('456')
    def test_unknown_token_is_not_owned(self): self.assertIsNone(self.client.get_order_by_client_id('555'))
    def test_exact_frontend_rejection_then_explicit_backup(self):
        self.wire.reject = True
        with self.assertRaises(OrderRejected): self.submit()
        self.assertEqual(len(self.stock_commands()), 1)
        self.wire.reject = False
        self.submit(route='CBATS', client_order_id='457')
        self.assertIn(' CBATS ', self.stock_commands()[-1])
    def test_rejection_survives_restart(self):
        self.wire.reject = True
        with self.assertRaises(OrderRejected): self.submit()
        self.client = self.make_client()
        with self.assertRaises(OrderRejected): self.client.get_order_by_client_id('456')
    def test_partial_rejection_remains_uncertain(self):
        self.wire.partial_reject = True
        with self.assertRaises(OrderSubmissionUncertain): self.submit()
        self.assertEqual(len(self.stock_commands()), 1)
    def test_conflicting_rejection_remains_uncertain(self):
        self.wire.conflicting_reject = True
        with self.assertRaises(OrderSubmissionUncertain): self.submit()
    def test_cancel_requires_later_confirmation(self):
        self.submit()
        self.client.cancel_order('20'); self.client.cancel_order('20')
        self.assertEqual(self.wire.commands.count('CANCEL 20'), 1)
        self.assertEqual(self.client.get_order('20')['status'], 'new')
        with self.assertRaises(DasError): self.client.cancel_order('ALL')
    def test_account_lock_across_different_journals(self):
        self.client.get_account()
        other = DasClient(self.settings, journal_path=self.path.with_name('other.json'),
                          socket_factory=lambda *_a, **_k: self.wire)
        self.addCleanup(other.close)
        with self.assertRaisesRegex(DasError, 'Another process'): other.get_account()
    def test_journal_identity_account_and_host_binding(self):
        self.client.get_account()
        self.client = self.make_client(account='OTHER')
        with self.assertRaisesRegex(DasError, 'another account'): self.client.get_account()
        self.client = self.make_client(host='192.0.2.7')
        with self.assertRaisesRegex(DasError, 'another account'): self.client.get_account()
    def test_montage_route_validation(self):
        for route in ['ALLROUTE', 'LIMIT', 'ARCAEL', 'CBATSL', 'ARCAE\nCANCEL']:
            with self.subTest(route=route), self.assertRaises(DasError): self.make_client(route=route)
    def test_changed_order_identity_blocks(self):
        self.submit(); self.wire.orders = [order_line(side='B')]
        with self.assertRaises(OrderSubmissionUncertain): self.client.get_order('20')
    def test_existing_borrow_has_no_purchase(self):
        self.wire.available = 100
        self.assertTrue(self.client.ensure_shortable('TEST', 100, .04)[0])
        self.assertFalse(self.paid_commands())
        self.assertTrue(self.client.validate_shortable('TEST', 100)[0])
    def test_prohibited_short_overrides_borrow(self):
        self.wire.short_info = '$SHORTINFO TEST Y 1000 Y 0 0 Y N'
        self.assertFalse(self.client.ensure_shortable('TEST', 100, .04)[0])
        self.assertFalse(self.paid_commands())
    def test_cheapest_capped_locate_once(self):
        result = self.client.ensure_shortable('TEST', 100, .04)
        self.assertEqual((result[0], result[2], result[3]), (True, 'LOCATE10', .01))
        self.assertEqual(len(self.paid_commands()), 1)
        self.assertTrue(self.paid_commands()[0].endswith(' 0.0100'))
        self.assertEqual(sum(c.endswith(' Reject') for c in self.wire.commands), 2)
    def test_explicit_offer_wins_after_minimum_fee(self):
        self.wire.minimums['LOCATE10'] = '9'
        result = self.client.ensure_shortable('TEST', 100, .04)
        self.assertEqual((result[0], result[2], result[3]), (True, 'LOCATE6', .02))
        self.assertEqual(len(self.paid_commands()), 1)
        self.assertTrue(self.paid_commands()[0].endswith(' Accept'))
    def test_locate10_below_minimum_not_sent_or_upsized(self):
        self.assertTrue(self.client.ensure_shortable('TEST', 99, .04)[0])
        self.assertFalse(any('LOCATE10' in c for c in self.wire.commands))
        self.assertTrue(any(c.startswith('SLNEWORDER TEST 99 LOCATE6') for c in self.wire.commands))
    def test_overpriced_routes_rejected(self):
        self.wire.minimums = dict.fromkeys(self.wire.minimums, '50')
        self.assertFalse(self.client.ensure_shortable('TEST', 100, .04)[0])
        self.assertFalse(self.paid_commands())
        self.assertEqual(sum(c.endswith(' Reject') for c in self.wire.commands), 2)
    def test_insufficient_quote_shares_excluded(self):
        self.wire.quote_available = 99
        self.assertEqual(self.client.ensure_shortable('TEST', 100, .04)[2], 'LOCATE6')
    def test_paid_timeout_restart_reconciles_without_duplicate(self):
        self.wire.purchase_no_ack = True
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.client = self.make_client()
        self.assertTrue(self.client.ensure_shortable('TEST', 100, .04)[0])
        self.assertEqual(len(self.paid_commands()), 1)
    def test_missing_paid_locate_blocks_existing_borrow_shortcut(self):
        self.wire.purchase_no_ack = True
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.wire.locates = {}; self.client = self.make_client()
        with self.assertRaisesRegex(DasError, 'unconfirmed'): self.client.ensure_shortable('TEST', 100, .04)
        self.assertEqual(len(self.paid_commands()), 1)
    def test_entry_deadline_blocks_paid_locate(self):
        with self.assertRaisesRegex(DasError, 'eligibility expired'):
            self.client.ensure_shortable('TEST', 100, .04, still_valid=lambda: False)
        self.assertFalse(self.paid_commands())
    def test_pause_after_intent_before_acceptance(self):
        self.wire.minimums['LOCATE10'] = '9'
        checks = iter([True, False])
        with self.assertRaisesRegex(DasError, 'eligibility expired'):
            self.client.ensure_shortable('TEST', 100, .04, still_valid=lambda: next(checks))
        self.assertFalse(self.paid_commands())
    def test_unowned_pending_locate_blocks(self):
        self.wire.locates['99'] = dict(id='99', qty=100, route='LOCATE4', token='777',
                                      price='0.02', status='Pending', open=100, filled=0)
        with self.assertRaisesRegex(DasError, 'unowned'): self.client.ensure_shortable('TEST', 100, .04)
        self.assertFalse(self.paid_commands())
    def test_unsolicited_offer_execution_blocks_purchase_and_survives_restart(self):
        def mutate(row, paid):
            if row['route'] == 'LOCATE4': row.update(status='Located', filled=row['qty'], open=0)
        self.wire.locate_mutator = mutate
        with self.assertRaisesRegex(DasError, 'without this client'):
            self.client.ensure_shortable('TEST', 100, .04)
        self.assertFalse(self.paid_commands())
        self.wire.locates = {}; self.client = self.make_client()
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.assertFalse(self.paid_commands())
    def test_wrong_identity_paid_locate_never_triggers_second_purchase(self):
        def mutate(row, paid):
            if paid: row['route'] = 'LOCATE6'
        self.wire.locate_mutator = mutate
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.client = self.make_client()
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.assertEqual(len(self.paid_commands()), 1)
    def test_nonfinite_locate_reply_is_not_a_free_quote(self):
        self.wire.prices['LOCATE10'] = 'nan'
        with self.assertRaises(DasError): self.client.ensure_shortable('TEST', 100, .04)
        self.assertFalse(self.paid_commands())
    def test_duplicate_order_token_blocks_reconciliation(self):
        self.wire.orders = [order_line(), order_line(oid='21')]
        with self.assertRaises(OrderSubmissionUncertain): self.client.get_order_by_client_id('456')
    def test_paid_intent_is_durable_before_command(self):
        def inspect(command):
            if command.startswith('SLNEWORDER') and ' LOCATE10 ' in command:
                row = next(iter(json.loads(self.path.read_text())['locates'].values()))
                self.assertEqual(row['state'], 'purchase_pending')
        self.wire.sent_callback = inspect
        self.client.ensure_shortable('TEST', 100, .04)


if __name__ == '__main__': unittest.main()
