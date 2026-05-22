"""POSIX file locks and per-slot path helpers for persistent state.

The state model itself (`MyState`) lives in `tf_project.models`. This module
keeps the on-disk plumbing — locks, slot-relative paths, decrypted-tfvars
context manager — and re-exports `MyState` for callers that historically
imported it from here.
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
from collections.abc import Iterator

from tf_project.config import Config
from tf_project.models import MyState
from tf_project.secrets import SecretsProvider

if sys.platform != "win32":
    import fcntl
else:  # pragma: no cover — Windows path
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
    """Atomically set the active slot to `slot`."""
    path = config.active_file
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(slot + "\n")


def clear_active(config: Config) -> None:
    """Remove the active-slot pointer. Safe to call if absent."""
    path = config.active_file
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _exclusive_lock(lock_path):
        path.unlink(missing_ok=True)


# ---- Decrypted-tfvars context manager (kept here for backward import paths) --


@contextlib.contextmanager
def decrypted_tfvars(state: MyState, provider: SecretsProvider) -> Iterator[pathlib.Path]:
    """Materialise `state.tfvars` through `provider`; yield the decrypted path."""
    with provider.materialize(state.tfvars) as path:
        yield path


__all__ = [
    "LockBusyError",
    "MyState",
    "clear_active",
    "decrypted_tfvars",
    "load_state",
    "read_active",
    "save_state",
    "try_exclusive_lock",
    "write_active",
]
