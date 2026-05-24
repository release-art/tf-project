"""POSIX file locks and per-slot persistence plumbing.

The state *model* (`MyState`) lives in `tf_project.models`. This module owns
the on-disk side: advisory locks, slot-scoped load/save, active-pointer
read/write.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import secrets
import sys
from collections.abc import Iterator

from tf_project.config import Config
from tf_project.models import MyState

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover - Windows path
    fcntl = None  # type: ignore[assignment]


class LockBusyError(RuntimeError):
    """Raised by `try_exclusive_lock` when the lock is already held."""


@contextlib.contextmanager
def _exclusive_lock(lock_path: pathlib.Path) -> Iterator[None]:
    """POSIX advisory lock on `lock_path`. No-op on platforms without fcntl."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover
        yield
        return
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def try_exclusive_lock(lock_path: pathlib.Path) -> Iterator[None]:
    """Non-blocking POSIX lock; raises LockBusyError if already held.

    No-op on platforms without fcntl (Windows).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover
        yield
        return
    with lock_path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusyError(f"Lock held by another process: {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---- Slot-scoped persistence --------------------------------------------------


def load_state(config: Config, slot: str) -> MyState | None:
    """Read `slots/<slot>/state.json` if it exists; None otherwise."""
    path = config.state_file_for(slot)
    if not path.exists():
        return None
    return MyState.load_file(path)


def save_state(state: MyState, config: Config) -> None:
    """Persist `state` to `slots/<state.slot>/state.json` under a flock."""
    path = config.state_file_for(state.slot)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        state.dump_file(path)


# ---- Active-slot pointer ------------------------------------------------------


def read_active(config: Config) -> str | None:
    """Read `tmp/active`; None if missing or empty."""
    path = config.active_file
    if not path.exists():
        return None
    value = path.read_text().strip()
    return value or None


def write_active(config: Config, slot: str) -> None:
    """Atomically set the active slot to `slot`.

    Writes to a sibling temp file then `Path.replace`s, so a concurrent
    reader in another shell never observes a half-written pointer
    (advisory flock alone doesn't protect non-cooperating readers).
    Tmp suffix is pid + random token to keep cross-container/cross-PID-
    namespace writes safe. Tmp is unlinked on any in-process error.
    """
    path = config.active_file
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            tmp.write_text(slot + "\n")
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def clear_active(config: Config) -> None:
    """Remove the active-slot pointer. Safe to call if absent."""
    path = config.active_file
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        path.unlink(missing_ok=True)


__all__ = [
    "LockBusyError",
    "clear_active",
    "load_state",
    "read_active",
    "save_state",
    "try_exclusive_lock",
    "write_active",
]
