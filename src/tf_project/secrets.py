"""Pluggable tfvars preprocessing (e.g. `op inject`)."""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Protocol

from tf_project.config import SecretsConfig


def _shred(path: pathlib.Path) -> None:
    """Best-effort secure delete: overwrite the file with zeros, then unlink.

    Filesystem semantics limit what "secure" means here — on copy-on-write
    filesystems (btrfs/zfs/APFS) the original blocks may persist. On ext4
    and tmpfs the bytes are overwritten in place. The unlink at the end is
    the authoritative cleanup; the overwrite is defence in depth.
    """
    with contextlib.suppress(OSError):
        if path.is_file():
            size = path.stat().st_size
            with path.open("r+b") as fh:
                fh.write(b"\x00" * size)
                fh.flush()
                os.fsync(fh.fileno())
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


class SecretsProvider(Protocol):
    @contextlib.contextmanager
    def materialize(self, src: pathlib.Path) -> Iterator[pathlib.Path]: ...


class NoopProvider:
    """Pass the tfvars file through unchanged."""

    @contextlib.contextmanager
    def materialize(self, src: pathlib.Path) -> Iterator[pathlib.Path]:
        yield src


class CommandProvider:
    """Run a configurable command that reads `{in}` and writes `{out}`."""

    def __init__(self, command: tuple[str, ...]) -> None:
        if not command:
            raise ValueError("CommandProvider requires a non-empty command")
        self._command = command

    @contextlib.contextmanager
    def materialize(self, src: pathlib.Path) -> Iterator[pathlib.Path]:
        if not src.is_file():
            raise FileNotFoundError(f"tfvars file not found: {src}")
        # Use a temporary *directory* and put the output file inside it.
        # We must not pre-create the file: `op inject --out-file <path>`
        # prompts "overwrite it? [Y/n]" if the path already exists.
        with tempfile.TemporaryDirectory(prefix="tfp-secrets-") as tmpdir:
            out_path = pathlib.Path(tmpdir) / f"{src.stem}.decrypted.tfvars"
            rendered = [arg.format(**{"in": str(src), "out": str(out_path)}) for arg in self._command]
            subprocess.check_call(rendered)
            try:
                yield out_path
            finally:
                # Explicitly shred the decrypted contents before the
                # tmpdir cleanup runs — the file holds plaintext secrets.
                _shred(out_path)


def provider_from_config(cfg: SecretsConfig) -> SecretsProvider:
    if not cfg.command:
        return NoopProvider()
    return CommandProvider(cfg.command)
