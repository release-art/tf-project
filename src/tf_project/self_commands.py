"""Implementations behind `tfp self ...` subcommands."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import tomllib
from typing import Any

from tf_project.config import (
    CONFIG_FILE_NAME,
    CONFIG_TABLE,
    DEFAULT_SECRETS_COMMAND,
    DEFAULT_TERRAFORM_DIR,
    DEFAULT_TFVARS_DIR,
    DEFAULT_TMP_DIR,
    PYPROJECT_FILE_NAME,
    PYPROJECT_TABLE,
    Config,
    ConfigNotFoundError,
)
from tf_project.state import MyState


class SelfCommandError(RuntimeError):
    """User-visible failure inside a `self` subcommand."""


@dataclasses.dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


_TABLE_PREFIX_PLACEHOLDER = "__TF_PROJECT_TABLE_PREFIX__"

DEFAULT_CONFIG_BODY = f"""\
terraform_dir = "{DEFAULT_TERRAFORM_DIR}"
tfvars_dir    = "{DEFAULT_TFVARS_DIR}"
tmp_dir       = "{DEFAULT_TMP_DIR}"
state_key_prefix = ""
# terraform_binary = "terraform"       # path or PATH-name; defaults to `which terraform`

[{_TABLE_PREFIX_PLACEHOLDER}.secrets]
# Pluggable tfvars preprocessor. Set `command = []` to disable.
command = {list(DEFAULT_SECRETS_COMMAND)!r}
"""


def _render_default_body(*, prefix: str) -> str:
    """Render the default config snippet with the right table-prefix.

    `prefix` is either ``"tf_project"`` (standalone file) or
    ``"tool.tf_project"`` (pyproject.toml).
    """
    header = f"[{prefix}]\n"
    return header + DEFAULT_CONFIG_BODY.replace(_TABLE_PREFIX_PLACEHOLDER, prefix)


def do_self_init(cwd: pathlib.Path) -> pathlib.Path:
    """Bootstrap a config in `cwd`. Refuses to overwrite an existing config.

    Returns the path of the file that was created or modified.
    """
    pyproject = cwd / PYPROJECT_FILE_NAME
    standalone = cwd / CONFIG_FILE_NAME

    if standalone.exists():
        raise SelfCommandError(f"{standalone} already exists; not overwriting.")

    if pyproject.exists():
        with pyproject.open("rb") as fin:
            data = tomllib.load(fin)
        if _has_table(data, PYPROJECT_TABLE):
            raise SelfCommandError(f"[{'.'.join(PYPROJECT_TABLE)}] already exists in {pyproject}; not overwriting.")
        snippet = _render_default_body(prefix=".".join(PYPROJECT_TABLE))
        existing = pyproject.read_text()
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        pyproject.write_text(existing + sep + snippet)
        return pyproject

    standalone.write_text(_render_default_body(prefix=CONFIG_TABLE))
    return standalone


def do_self_config_print(config: Config, *, as_json: bool) -> str:
    payload = _config_to_dict(config)
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = []
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k} = {v}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines)


def do_self_config_path(cwd: pathlib.Path) -> pathlib.Path:
    """Return the file Config.discover would pick up from `cwd`."""
    cwd = cwd.resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
    for parent in [cwd, *cwd.parents]:
        candidate = parent / PYPROJECT_FILE_NAME
        if not candidate.is_file():
            continue
        with candidate.open("rb") as fin:
            data = tomllib.load(fin)
        if _has_table(data, PYPROJECT_TABLE):
            return candidate
    raise ConfigNotFoundError(
        f"No tf_project config found by walking up from {cwd}. Run `tfp self init` to create one."
    )


def do_self_state_show(config: Config) -> str:
    state = MyState.load(config)
    if state is None:
        raise SelfCommandError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return json.dumps(dataclasses.asdict(state), indent=2, sort_keys=True)


def do_self_state_clear(config: Config) -> bool:
    """Remove the saved state file. Returns True if a file was removed."""
    if not config.state_file.exists():
        return False
    config.state_file.unlink()
    return True


def do_self_doctor(config: Config) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    checks.append(_terraform_binary_check(config.terraform_binary))

    if config.secrets.command:
        secrets_bin = config.secrets.command[0]
        resolved = shutil.which(secrets_bin)
        checks.append(
            DoctorCheck(
                name=f"secrets command `{secrets_bin}` on PATH",
                ok=resolved is not None,
                detail=resolved or f"not found; install `{secrets_bin}` or disable in [tf_project.secrets]",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="secrets command",
                ok=True,
                detail="disabled (command = [])",
            )
        )

    checks.append(_dir_check("terraform_dir", config.terraform_dir))
    checks.append(_dir_check("tfvars_dir", config.tfvars_dir))
    checks.append(_dir_check("tmp_dir", config.tmp_dir, allow_missing=True))

    return checks


def _terraform_binary_check(binary: str) -> DoctorCheck:
    name = f"terraform binary `{binary}`"
    if "/" in binary or "\\" in binary:
        path = pathlib.Path(binary)
        if path.is_file():
            return DoctorCheck(name=name, ok=True, detail=str(path))
        return DoctorCheck(name=name, ok=False, detail=f"{path} (not a file)")
    resolved = shutil.which(binary)
    if resolved is not None:
        return DoctorCheck(name=name, ok=True, detail=resolved)
    return DoctorCheck(name=name, ok=False, detail="not found on PATH; install or set `terraform_binary`")


def _dir_check(name: str, path: pathlib.Path, *, allow_missing: bool = False) -> DoctorCheck:
    if path.is_dir():
        return DoctorCheck(name=f"{name} exists", ok=True, detail=str(path))
    if allow_missing:
        return DoctorCheck(
            name=f"{name} exists",
            ok=True,
            detail=f"{path} (missing — will be created on demand)",
        )
    return DoctorCheck(name=f"{name} exists", ok=False, detail=str(path))


def _has_table(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    cursor: Any = data
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return False
        cursor = cursor[key]
    return isinstance(cursor, dict)


def _config_to_dict(config: Config) -> dict[str, Any]:
    return {
        "project_root": str(config.project_root),
        "terraform_dir": str(config.terraform_dir),
        "tfvars_dir": str(config.tfvars_dir),
        "tmp_dir": str(config.tmp_dir),
        "state_key_prefix": config.state_key_prefix,
        "state_file": str(config.state_file),
        "tfplan_file": str(config.tfplan_file),
        "terraform_binary": config.terraform_binary,
        "secrets": {"command": list(config.secrets.command)},
    }


__all__ = [
    "DoctorCheck",
    "SelfCommandError",
    "do_self_config_path",
    "do_self_config_print",
    "do_self_doctor",
    "do_self_init",
    "do_self_state_clear",
    "do_self_state_show",
]
