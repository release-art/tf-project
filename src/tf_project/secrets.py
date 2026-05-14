"""Pluggable tfvars preprocessing (e.g. `op inject`)."""

from __future__ import annotations

import contextlib
import pathlib
import subprocess
import tempfile
from collections.abc import Iterator
from typing import Protocol

from tf_project.config import SecretsConfig


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
        with tempfile.NamedTemporaryFile("w", suffix=".tfvars", delete=False) as fout:
            out_path = pathlib.Path(fout.name)
        try:
            rendered = [arg.format(**{"in": str(src), "out": str(out_path)}) for arg in self._command]
            subprocess.check_call(rendered)
            yield out_path
        finally:
            with contextlib.suppress(FileNotFoundError):
                out_path.unlink()


def provider_from_config(cfg: SecretsConfig) -> SecretsProvider:
    if not cfg.command:
        return NoopProvider()
    return CommandProvider(cfg.command)
