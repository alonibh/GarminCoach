"""Single-host advisory process lock for the GarminCoach service."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import BinaryIO

import config


@dataclass
class ProcessLock:
    path: Path
    handle: BinaryIO
    released: bool = False


def acquire_process_lock(path: Path | str | None = None) -> ProcessLock:
    lock_path = Path(path or (config.PROJECT_ROOT / "garmincoach.lock")).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise RuntimeError(
            "Another GarminCoach process already holds the local process lock"
        ) from exc
    return ProcessLock(lock_path, handle)


def release_process_lock(lock: ProcessLock | None) -> None:
    if lock is None or lock.released:
        return
    try:
        lock.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.handle.close()
        lock.released = True
