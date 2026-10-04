"""Atomic, account-bound live state with a single-process ownership lock."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path


class StateLockError(RuntimeError):
    """Another supervisor still holds the OS lock for this state."""


class StateStore:
    def __init__(self, path: Path, identity: str):
        self.path, self.identity, self._lock = path, identity, None

    def open(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, 2) == 0:
                    handle.write(b"0"); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            owner_pid = None
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                try:
                    handle.seek(0)
                    owner = json.loads(handle.read(4096))
                    if isinstance(owner, dict) and type(owner.get("pid")) is int and owner["pid"] > 0:
                        owner_pid = owner["pid"]
                except (OSError, ValueError):
                    pass
            handle.close()
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            owner_hint = f" Last recorded owner: PID {owner_pid}." if owner_pid else ""
            raise StateLockError(
                "Another 4am short process owns this live state."
                f"{owner_hint} Lock: {lock_path}. "
                "Use the existing dashboard or inspect the owning process before restarting. "
                "A suspended process (Ctrl-Z) still holds this lock and cannot supervise exits. "
                "Do not delete the lock file or change state directories to bypass it."
            ) from None
        self._lock = handle
        try:
            # Diagnostics only: stale metadata never substitutes for the OS lock.
            # The record also supplies a byte for subsequent Windows locking.
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid()}).encode("utf-8"))
            handle.flush()
        except BaseException:
            self.close()
            raise
        try:
            if not self.path.exists():
                return {"version": 1, "identity": self.identity, "days": {}, "events": []}
            value = json.loads(self.path.read_text())
            if not isinstance(value, dict) or value.get("version") != 1 or value.get("identity") != self.identity or not isinstance(value.get("days"), dict):
                raise ValueError
            return value
        except (OSError, ValueError):
            self.close()
            raise RuntimeError("Live state is unreadable or belongs to another execution account; automatic entries are blocked") from None

    def save(self, value: dict) -> None:
        if self._lock is None:
            raise RuntimeError("Live state ownership has not been acquired")
        value.update(version=1, identity=self.identity)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".4am_short_", delete=False) as handle:
                temporary = handle.name
                json.dump(value, handle, allow_nan=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def close(self) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None
