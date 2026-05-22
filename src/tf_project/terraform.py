"""Helpers for assembling and running terraform invocations."""

from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import pathlib
import shlex
import subprocess
import sys
from collections.abc import Iterable

# Re-exports for backward compatibility — banner parsing lives in
# `tf_project.banner` but external callers (and existing tests) import it
# from here.
from tf_project.banner import (  # noqa: F401
    BannerError,
    ProjectInfoNotFoundError,
)
from tf_project.models import LastInvocation

log = logging.getLogger("tf_project.terraform")


class TerraformExit(SystemExit):
    """Raised to abort with a specific exit code without a Python traceback.

    `cli.main()` catches this and forwards the code to `sys.exit`.
    """


@dataclasses.dataclass(slots=True)
class _RuntimeOptions:
    dry_run: bool = False
    verbose: bool = False
    last_invocation_path: pathlib.Path | None = None


_options = _RuntimeOptions()


def set_runtime_options(
    *,
    dry_run: bool = False,
    verbose: bool = False,
    last_invocation_path: pathlib.Path | None = None,
) -> None:
    """Set process-wide flags consulted by `run` and `exec_passthrough`."""
    _options.dry_run = dry_run
    _options.verbose = verbose
    _options.last_invocation_path = last_invocation_path


def _format_argv(cmd: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in cmd)


def _announce(cmd: list[str]) -> None:
    if _options.verbose or _options.dry_run:
        prefix = "[dry-run]" if _options.dry_run else "$"
        print(f"{prefix} {_format_argv(cmd)}", file=sys.stderr)


def _record_invocation(cmd: list[str], *, exit_code: int | None) -> None:
    path = _options.last_invocation_path
    if path is None:
        return
    try:
        last = LastInvocation(
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            argv=cmd,
            exit_code=exit_code,
        )
        last.dump_file(path)
    except OSError:
        # Recording is best-effort; don't crash the real command for it.
        pass


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
        _record_invocation(cmd, exit_code=127)
        raise TerraformExit(127) from exc
    while True:
        try:
            rc = proc.wait()
            break
        except KeyboardInterrupt:
            continue
    _record_invocation(cmd, exit_code=rc)
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
    _record_invocation(cmd, exit_code=None)
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


def merged_env(
    extra: dict[str, str],
    *,
    data_dir: pathlib.Path | None = None,
    plugin_cache_dir: pathlib.Path | None = None,
) -> dict[str, str]:
    """Build the env for a terraform spawn.

    Layered on top of `os.environ` (in order):
      - `extra` (slot's banner-declared env)
      - `TF_DATA_DIR` if `data_dir` is given (absolute, see config layer)
      - `TF_PLUGIN_CACHE_DIR` + `TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE`
        if `plugin_cache_dir` is given.

    All path values must be absolute by the time they reach this function.
    """
    out = os.environ.copy()
    out.update(extra)
    if data_dir is not None:
        out["TF_DATA_DIR"] = str(data_dir)
    if plugin_cache_dir is not None:
        out["TF_PLUGIN_CACHE_DIR"] = str(plugin_cache_dir)
        out["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] = "1"
    return out
