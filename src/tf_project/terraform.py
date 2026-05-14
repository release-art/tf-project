"""Helpers for assembling and running terraform invocations."""

from __future__ import annotations

import dataclasses
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Iterable

# Re-exports for backward compatibility — banner parsing now lives in
# `tf_project.banner` but external callers (and existing tests) import it
# from here.
from tf_project.banner import (  # noqa: F401
    BannerError,
    ProjectInfoNotFoundError,
    find_project_info,
)

log = logging.getLogger("tf_project.terraform")


class TerraformExit(SystemExit):
    """Raised to abort with a specific exit code without a Python traceback.

    `cli.main()` catches this and forwards the code to `sys.exit`.
    """


@dataclasses.dataclass(slots=True)
class _RuntimeOptions:
    dry_run: bool = False
    verbose: bool = False


_options = _RuntimeOptions()


def set_runtime_options(*, dry_run: bool = False, verbose: bool = False) -> None:
    """Set process-wide flags consulted by `run` and `exec_passthrough`."""
    _options.dry_run = dry_run
    _options.verbose = verbose


def _format_argv(cmd: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in cmd)


def _announce(cmd: list[str]) -> None:
    if _options.verbose or _options.dry_run:
        prefix = "[dry-run]" if _options.dry_run else "$"
        print(f"{prefix} {_format_argv(cmd)}", file=sys.stderr)


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Run a terraform subprocess, forwarding signals and exit code.

    SIGINT during a long-running terraform command (e.g. `apply`) is
    delivered to the whole process group by the controlling terminal, so
    terraform sees it independently. We catch `KeyboardInterrupt` here and
    re-wait so terraform can finish its own cleanup before we return.

    A non-zero exit raises `TerraformExit(code)`, which `cli.main()`
    translates into the process exit code with no traceback.
    """
    _announce(cmd)
    if _options.dry_run:
        return
    try:
        proc = subprocess.Popen(cmd, env=env)
    except FileNotFoundError as exc:
        print(f"{cmd[0]}: command not found", file=sys.stderr)
        raise TerraformExit(127) from exc
    while True:
        try:
            rc = proc.wait()
            break
        except KeyboardInterrupt:
            # terraform got SIGINT too; let it finish cleanly.
            continue
    if rc != 0:
        raise TerraformExit(rc)


def exec_passthrough(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Replace this process with a terraform invocation.

    Used for the bare passthrough path so signal handling and exit code are
    fully native — no Python in the loop.
    """
    _announce(cmd)
    if _options.dry_run:
        return
    try:
        if env is None:
            os.execvp(cmd[0], cmd)
        else:
            os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError as exc:
        print(f"{cmd[0]}: command not found", file=sys.stderr)
        raise TerraformExit(127) from exc


def target_args(targets: Iterable[str] | None) -> list[str]:
    return [f"-target={t}" for t in (targets or [])]


def replace_args(replaces: Iterable[str] | None) -> list[str]:
    return [f"-replace={r}" for r in (replaces or [])]


def merged_env(extra: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    out.update(extra)
    return out
