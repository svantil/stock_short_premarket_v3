"""Atomic, account-bound live state with a single-process ownership lock."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StateStore:
    def __init__(self, path: Path, identity: str):
        self.path, self.identity, self._lock = path, identity, None

    def open(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.with_suffix(".lock").open("a+b")
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
        except OSError:
            handle.close()
            raise RuntimeError("Another 4am short process owns this live state") from None
        self._lock = handle
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
