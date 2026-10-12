"""OS-held process lease (singleton guard) — Windows + POSIX.

A file lock held for the lifetime of the acquiring process. The OS releases the
lock automatically on process death, so a stale lockfile is never mistaken for a
live owner, and there is no PID-file race. A second process attempting to
acquire the same lease fails immediately (non-blocking).

Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` on POSIX.
"""

from __future__ import annotations

import os
from pathlib import Path


class ProcessLease:
    """Exclusive advisory lock on a lease file, held until release() or death."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to acquire the lease. Returns False if another process holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                import msvcrt

                # msvcrt.locking locks a byte range from the current position;
                # ensure the file has at least one byte to lock.
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """Release the lease (also released automatically on process exit)."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ProcessLease":
        if not self.acquire():
            raise RuntimeError(f"process lease already held: {self.path}")
        return self

    def __exit__(self, *exc) -> bool:
        self.release()
        return False