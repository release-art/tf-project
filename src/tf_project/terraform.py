"""Helpers for assembling terraform invocations."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from collections.abc import Iterable
from typing import Any


class ProjectInfoNotFoundError(RuntimeError):
    """No `# {...}` JSON banner with `header == "terraform"` found in tfvars."""


def find_project_info(tfvars: pathlib.Path) -> dict[str, Any]:
    with tfvars.open("r") as fin:
        for line in fin:
            if not line.startswith("#"):
                continue
            maybe_json = line.lstrip("#").strip()
            try:
                data = json.loads(maybe_json)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("header") == "terraform":
                return data
    raise ProjectInfoNotFoundError(
        f"Project info banner not found in {tfvars}. "
        'Expected a comment line like `# {"header": "terraform", "project": "<name>"}`.'
    )


def target_args(targets: Iterable[str] | None) -> list[str]:
    return [f"-target={t}" for t in (targets or [])]


def replace_args(replaces: Iterable[str] | None) -> list[str]:
    return [f"-replace={r}" for r in (replaces or [])]


def merged_env(extra: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    out.update(extra)
    return out


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Wrapper around `subprocess.check_call`. Indirected so tests can mock one place."""
    subprocess.check_call(cmd, env=env)
