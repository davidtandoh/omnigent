"""Kernel leases whose inode remains available for release proofs."""

from __future__ import annotations

import os

from filelock import UnixFileLock


class PersistentFileLock(UnixFileLock):
    """Release the flock without deleting its durable identity file."""

    def _release(self) -> None:
        import fcntl

        fd = self._context.lock_file_fd
        assert fd is not None
        self._context.lock_file_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
