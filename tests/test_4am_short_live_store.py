"""State ownership checks use temporary files and isolated child processes."""

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from four_am_short.live.store import StateLockError, StateStore


PROJECT = Path(__file__).resolve().parents[1]


class StateStoreOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        self.lock_path = self.path.with_suffix(".lock")

    def store(self):
        store = StateStore(self.path, "test-account")
        self.addCleanup(store.close)
        return store

    def test_contention_identifies_owner_and_preserves_metadata(self):
        first = self.store()
        state = first.open()
        first.save(state)
        # Windows' mandatory byte-range lock can also prevent diagnostic reads.
        metadata = self.lock_path.read_bytes() if os.name != "nt" else None
        if metadata is not None:
            self.assertEqual(json.loads(metadata)["pid"], os.getpid())
        original_state = self.path.read_bytes()

        second = self.store()
        with self.assertRaises(StateLockError) as failure:
            second.open()

        self.assertIsInstance(failure.exception, RuntimeError)
        self.assertIn("Another", str(failure.exception))
        if os.name != "nt":
            self.assertIn(f"PID {os.getpid()}", str(failure.exception))
        self.assertIn(str(self.lock_path), str(failure.exception))
        if metadata is not None:
            self.assertEqual(self.lock_path.read_bytes(), metadata)
        self.assertEqual(self.path.read_bytes(), original_state)
        self.assertIsNone(second._lock)

        first.close()
        self.assertEqual(json.loads(self.lock_path.read_bytes())["pid"], os.getpid())
        self.assertEqual(second.open(), state)

    def test_unlocked_file_with_stale_metadata_does_not_block_open(self):
        # Even a PID that currently exists is only diagnostic metadata.
        self.lock_path.write_text(json.dumps({"pid": os.getpid(), "old": "metadata"}))
        store = self.store()
        self.assertEqual(store.open()["identity"], "test-account")
        store.close()
        self.assertEqual(json.loads(self.lock_path.read_bytes())["pid"], os.getpid())
        self.assertNotIn("old", json.loads(self.lock_path.read_bytes()))

    def test_unlocked_legacy_or_malformed_metadata_is_replaced(self):
        for metadata in (b"", b"0", b"not json", b'{"pid":'):
            with self.subTest(metadata=metadata):
                self.lock_path.write_bytes(metadata)
                store = self.store()
                store.open()
                store.close()
                self.assertEqual(json.loads(self.lock_path.read_bytes())["pid"], os.getpid())

    def test_non_contention_lock_error_propagates_and_closes_handle(self):
        failure = OSError(errno.EIO, "test lock I/O failure")
        lock_operation = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        store = self.store()
        handle = self.lock_path.open("a+b")
        self.addCleanup(handle.close)
        with mock.patch.object(Path, "open", return_value=handle):
            with mock.patch(lock_operation, side_effect=failure):
                with self.assertRaises(OSError) as raised:
                    store.open()

        self.assertIs(raised.exception, failure)
        self.assertTrue(handle.closed)
        self.assertIsNone(store._lock)
        self.store().open()

    def test_metadata_write_failure_releases_lock_and_preserves_error(self):
        # Prepopulate so Windows' initial byte write is not the failure point.
        self.lock_path.write_bytes(b"0")
        handle = self.lock_path.open("a+b")
        self.addCleanup(handle.close)
        failing_handle = mock.Mock(wraps=handle)
        failure = OSError(errno.ENOSPC, "test metadata disk full")
        failing_handle.write.side_effect = failure
        store = self.store()
        with mock.patch.object(Path, "open", return_value=failing_handle):
            with self.assertRaises(OSError) as raised:
                store.open()

        self.assertIs(raised.exception, failure)
        self.assertTrue(handle.closed)
        self.assertIsNone(store._lock)
        self.store().open()

    def test_child_owns_lock_until_process_exit_without_explicit_close(self):
        ready_path = self.path.with_suffix(".ready")
        script = """
import os
from pathlib import Path
import sys
from four_am_short.live.store import StateStore
path = Path(sys.argv[1])
store = StateStore(path, "test-account")
store.open()
path.with_suffix(".ready").write_text(str(os.getpid()))
sys.stdin.readline()
os._exit(0)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(self.path)],
            cwd=PROJECT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready_path.exists(), "Child did not acquire its temporary state lock")
            self.assertEqual(int(ready_path.read_text()), child.pid)
            contender = self.store()
            with self.assertRaises(StateLockError) as failure:
                contender.open()
            if os.name != "nt":
                self.assertIn(f"PID {child.pid}", str(failure.exception))
            metadata = self.lock_path.read_bytes() if os.name != "nt" else None

            child.communicate(input="exit\n", timeout=5)
            self.assertEqual(child.returncode, 0)
            if metadata is not None:
                self.assertEqual(self.lock_path.read_bytes(), metadata)
            self.assertEqual(json.loads(self.lock_path.read_bytes())["pid"], child.pid)
            self.assertEqual(contender.open()["identity"], "test-account")
            contender.close()
            self.assertEqual(json.loads(self.lock_path.read_bytes())["pid"], os.getpid())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
