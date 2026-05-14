"""Sanity checks against the installed CLI entry point via a real subprocess.

These complement the in-process `CliRunner` tests by exercising the
`python -m tf_project` invocation path and confirming that exit codes and
stdout flow through correctly after install.
"""

from __future__ import annotations

import subprocess
import sys

from tf_project.__version__ import __version__


def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tf_project", *args],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )


def test_version_via_module() -> None:
    result = _run(["--version"])
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_help_via_module() -> None:
    result = _run(["--help"])
    assert result.returncode == 0
    assert "Terraform" in result.stdout
    assert "status" in result.stdout


def test_passthrough_dry_run_does_not_invoke_terraform(tmp_path: object) -> None:
    """`--dry-run` should print the argv to stderr and exit 0 without invoking terraform."""
    # No config in cwd → exit 1 with a config-not-found error. We bypass that
    # by ensuring we run from an unconfigured CWD only after creating a config
    # there. Use `tfp self init` first.
    import pathlib

    cwd = pathlib.Path(str(tmp_path))
    init_result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tf_project", "self", "init"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init_result.returncode == 0, init_result.stderr

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "tf_project", "--dry-run", "validate", "-json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stderr
    assert "validate" in result.stderr
