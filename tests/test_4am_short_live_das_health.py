"""Connection health is observable without taking the DAS socket lock."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from four_am_short.live.config import DasSettings, load_live_settings
from four_am_short.live.das import DasClient, DasError
from test_4am_short_live_das import Wire


class DasHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'health.json'
        self.wire = Wire()
        self.settings = DasSettings(username='TESTUSER', password='test-only-password',
                                    account='TESTACCOUNT', timeout_seconds=.1)
        self.client = self.make_client()

    def make_client(self, factory=None):
        if hasattr(self, 'client'):
            self.client.close()
        client = DasClient(self.settings, journal_path=self.path,
                           socket_factory=factory or (lambda *_a, **_kw: self.wire))
        self.addCleanup(client.close)
        return client

    def replace_bp(self, response):
        normal_send = self.wire.sendall
        def send(data):
            if data.decode().strip() == 'GET BP':
                self.wire.commands.append('GET BP')
                self.wire.queue(response)
            else:
                normal_send(data)
        self.wire.sendall = send

    def test_initial_status_is_disconnected_and_snapshot_is_independent(self):
        status = self.client.connection_status
        self.assertFalse(status['connected'] or status['checking'] or status['connecting'])
        self.assertIsNone(status['last_checked_at'])
        self.assertIsNone(status['last_connected_at'])
        self.assertIsNone(status['last_disconnected_at'])
        status['connected'] = True
        self.assertFalse(self.client.connection_status['connected'])
        self.assertEqual(self.wire.commands, [])

    def test_login_is_not_connected_until_completed_snapshots_and_valid_bp(self):
        observed = []
        self.wire.sent_callback = lambda command: observed.append((command, self.client.connection_status))
        self.client.get_account()
        first_bp = next(status for command, status in observed if command == 'GET BP')
        self.assertFalse(first_bp['connected'])
        self.assertTrue(first_bp['checking'] and first_bp['connecting'])
        status = self.client.connection_status
        self.assertTrue(status['connected'])
        self.assertFalse(status['checking'] or status['connecting'])
        self.assertIsNone(status['last_error'])
        self.assertEqual(datetime.fromisoformat(status['last_checked_at']).utcoffset().total_seconds(), 0)
        self.assertIsNotNone(status['last_connected_at'])
        self.assertFalse(any(command.startswith(('NEWORDER', 'SLNEWORDER', 'CANCEL')) for command in self.wire.commands))

    def test_successful_health_check_refreshes_time_without_new_login(self):
        self.client.get_account()
        before = self.client.connection_status
        self.client.get_account()
        after = self.client.connection_status
        self.assertGreaterEqual(after['last_checked_at'], before['last_checked_at'])
        self.assertGreaterEqual(after['last_connected_at'], before['last_connected_at'])
        self.assertEqual(after['last_connected_at'], after['last_checked_at'])
        self.assertEqual(sum(command.startswith('LOGIN ') for command in self.wire.commands), 1)

    def test_upstream_disconnect_immediately_invalidates_cached_connected_status(self):
        self.client.get_account()
        self.wire.queue('#OrderServer:Missing heartbeat')
        with self.assertRaisesRegex(DasError, 'heartbeat'):
            self.client.get_account()
        status = self.client.connection_status
        self.assertFalse(status['connected'] or status['checking'] or status['connecting'])
        self.assertIn('heartbeat', status['last_error'])
        self.assertIsNotNone(status['last_disconnected_at'])
        self.assertIsNotNone(status['last_connected_at'])
        self.assertIsNone(self.client._socket)

    def test_timeout_invalidates_then_next_readonly_check_reconnects(self):
        self.client.get_account()
        self.wire.omit_end = True
        with self.assertRaises(DasError):
            self.client.position_qty('TEST')
        down = self.client.connection_status
        self.assertFalse(down['connected'])
        self.assertIn('timeout', down['last_error'])
        self.wire.omit_end = False
        self.client.get_account()
        recovered = self.client.connection_status
        self.assertTrue(recovered['connected'])
        self.assertIsNone(recovered['last_error'])
        self.assertEqual(recovered['last_disconnected_at'], down['last_disconnected_at'])
        self.assertGreaterEqual(recovered['last_connected_at'], down['last_connected_at'])
        self.assertEqual(sum(command.startswith('LOGIN ') for command in self.wire.commands), 2)

    def test_authentication_failure_has_timestamp_and_useful_error(self):
        self.wire.login_failed = True
        with self.assertRaises(DasError):
            self.client.get_account()
        status = self.client.connection_status
        self.assertFalse(status['connected'])
        self.assertIn('logon failed', status['last_error'])
        self.assertIsNotNone(status['last_checked_at'])
        self.assertIsNone(status['last_connected_at'])

    def test_connection_refusal_never_leaks_raw_exception(self):
        def refused(*args, **kwargs):
            raise ConnectionRefusedError('test-only-password TESTUSER TESTACCOUNT')
        self.client = self.make_client(refused)
        with self.assertRaisesRegex(DasError, 'refused'):
            self.client.get_account()
        status = self.client.connection_status
        self.assertFalse(status['connected'])
        self.assertIn('No login was sent', status['last_error'])
        self.assertNotIn('test-only-password', status['last_error'])
        self.assertIsNone(status['last_connected_at'])

    def test_invalid_initial_buying_power_never_marks_connected(self):
        for reply in ('BP', 'BP nan', 'BP -1'):
            with self.subTest(reply=reply):
                self.wire = Wire()
                self.client = self.make_client()
                self.replace_bp(reply)
                with self.assertRaises(DasError):
                    self.client.get_account()
                status = self.client.connection_status
                self.assertFalse(status['connected'])
                self.assertIsNone(status['last_connected_at'])
                self.assertIsNotNone(status['last_error'])

    def test_invalid_health_check_reply_clears_previously_connected_status(self):
        self.client.get_account()
        self.replace_bp('BP nan')
        with self.assertRaises(DasError):
            self.client.get_account()
        self.assertFalse(self.client.connection_status['connected'])

    def test_send_failure_invalidates_health(self):
        self.client.get_account()
        def failed_send(data):
            raise OSError('test-only-password')
        self.wire.sendall = failed_send
        with self.assertRaises(DasError):
            self.client.get_account()
        status = self.client.connection_status
        self.assertFalse(status['connected'])
        self.assertIn('sending command', status['last_error'])
        self.assertNotIn('test-only-password', status['last_error'])

    def test_close_invalidates_before_socket_close_executes(self):
        self.client.get_account()
        def close():
            self.assertFalse(self.client.connection_status['connected'])
            raise OSError('socket close failed')
        self.wire.close = close
        self.client.close()
        self.assertFalse(self.client.connection_status['connected'])
        self.assertIsNone(self.client._socket)

    def test_health_snapshot_does_not_acquire_socket_lock(self):
        locked, release = threading.Event(), threading.Event()
        def hold_socket_lock():
            with self.client._lock:
                locked.set()
                release.wait(timeout=2)
        worker = threading.Thread(target=hold_socket_lock)
        worker.start()
        try:
            self.assertTrue(locked.wait(timeout=1))
            began = time.monotonic()
            status = self.client.connection_status
            self.assertLess(time.monotonic() - began, .5)
            self.assertFalse(status['connected'])
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_error_snapshot_redacts_credentials_and_controls(self):
        self.client._disconnect('Failed TESTUSER test-only-password TESTACCOUNT\nstatus')
        error = self.client.connection_status['last_error']
        for secret in ('TESTUSER', 'test-only-password', 'TESTACCOUNT', '\n'):
            self.assertNotIn(secret, error)
        self.assertIn('[redacted]', error)


class DasHealthConfigTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'stocks.csv').write_text('2026-09-21 TEST\n')
        (self.root / 'backtest.json').write_text(json.dumps({'input_file': 'stocks.csv'}))
        self.path = self.root / 'live.json'

    def load(self, das):
        self.path.write_text(json.dumps({'strategy_config': 'backtest.json', 'das': das}))
        with patch.dict(os.environ, {}, clear=True):
            return load_live_settings(self.path)

    def test_health_defaults_and_explicit_json_settings(self):
        defaults = self.load({})
        self.assertEqual(defaults.das.health_check_seconds, 10)
        self.assertEqual(defaults.das.reconnect_seconds, 5)
        settings = self.load({'health_check_seconds': 12.5, 'reconnect_seconds': 7})
        self.assertEqual(settings.das.health_check_seconds, 12.5)
        self.assertEqual(settings.das.reconnect_seconds, 7)
        self.assertEqual(settings.public_rules()['das_health_check_seconds'], 12.5)
        self.assertEqual(settings.public_rules()['das_reconnect_seconds'], 7)

    def test_invalid_health_intervals_are_rejected(self):
        for field in ('health_check_seconds', 'reconnect_seconds'):
            for value in (0, -1, True, '5', None, float('nan'), float('inf')):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.load({field: value})
