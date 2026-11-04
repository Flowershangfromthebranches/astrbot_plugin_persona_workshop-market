"""Process-lifetime lease on a data directory for background-work recovery.

Every CLI command and MCP server initialises the container.  Startup recovery
(failing orphaned jobs, parking interrupted rooms, re-queueing memory work)
must only run in a process that can be sure nothing else is executing that
work.  The lease is an advisory ``flock`` held for the process lifetime: a
running Web server keeps it, so a CLI command never touches its live jobs,
and the OS releases it when a process dies, so the next start still recovers.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import IO

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no advisory flock
    fcntl = None  # type: ignore[assignment]


class WorkerLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lease if no other live process holds it."""

        if self._handle is not None:
            return True
        if fcntl is None:
            # Without advisory locks keep the historical behaviour: recover.
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
