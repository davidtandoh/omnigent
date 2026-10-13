"""Bounded regular-file snapshots and canonical lifecycle record writes."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path


def read_regular_file(path: Path, *, limit: int) -> bytes:
    """Read one bounded snapshot without following the leaf symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid_state_file")
        content = handle.read(limit + 1)
        if len(content) > limit:
            raise ValueError("invalid_state_file")
        return content


def canonical_json(value: object) -> str:
    """Serialize a machine-consumed lifecycle record."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"


def write_state_file(path: Path, value: object) -> None:
    """Atomically replace a private record; callers own its operation lock."""
    write_regular_file(path, canonical_json(value))


def write_regular_file(path: Path, content: str) -> None:
    """Atomically write a private file in an already-owned directory."""
    fd, temporary = tempfile.mkstemp(prefix=".record-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
