"""Discovery and loading of `tf_project` configuration."""

from __future__ import annotations

import dataclasses
import pathlib
import shutil
import tomllib
from typing import Any

CONFIG_FILE_NAME = "tf_project.toml"
PYPROJECT_FILE_NAME = "pyproject.toml"
PYPROJECT_TABLE = ("tool", "tf_project")
CONFIG_TABLE = "tf_project"

DEFAULT_STATE_FILE_NAME = "my_terraform_state.json"
DEFAULT_TFPLAN_NAME = "my.tfplan"
DEFAULT_TERRAFORM_DIR = "terraform"
DEFAULT_TFVARS_DIR = "tfvars"
DEFAULT_TMP_DIR = "tmp"
DEFAULT_TERRAFORM_BINARY = "terraform"

DEFAULT_SECRETS_COMMAND: tuple[str, ...] = (
    "op",
    "inject",
    "--in-file",
    "{in}",
    "--out-file",
    "{out}",
)


class ConfigError(Exception):
    """Raised when the configuration is malformed."""


class ConfigNotFoundError(ConfigError):
    """Raised when no configuration could be located by walking up from cwd."""


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class SecretsConfig:
    command: tuple[str, ...] = DEFAULT_SECRETS_COMMAND


@dataclasses.dataclass(frozen=True, kw_only=True, slots=True)
class Config:
    project_root: pathlib.Path
    terraform_dir: pathlib.Path
    tfvars_dir: pathlib.Path
    tmp_dir: pathlib.Path
    state_key_prefix: str
    state_file: pathlib.Path
    tfplan_file: pathlib.Path
    terraform_binary: str
    secrets: SecretsConfig

    @classmethod
    def discover(cls, start: pathlib.Path | None = None) -> "Config":
        start = (start or pathlib.Path.cwd()).resolve()
        for parent in [start, *start.parents]:
            candidate = parent / CONFIG_FILE_NAME
            if candidate.is_file():
                return cls._from_toml(candidate, table=(CONFIG_TABLE,), project_root=parent)
        for parent in [start, *start.parents]:
            candidate = parent / PYPROJECT_FILE_NAME
            if candidate.is_file():
                with candidate.open("rb") as fin:
                    data = tomllib.load(fin)
                table = _walk(data, PYPROJECT_TABLE)
                if table is not None:
                    return cls._build(table, project_root=parent)
        raise ConfigNotFoundError(
            f"Could not find {CONFIG_FILE_NAME} or [tool.tf_project] in pyproject.toml "
            f"by walking up from {start}.\n\n"
            f"Create a {CONFIG_FILE_NAME} at your project root, e.g.:\n\n"
            f"  [tf_project]\n"
            f'  terraform_dir = "terraform"\n'
            f'  tfvars_dir = "tfvars"\n'
            f'  tmp_dir = "tmp"\n'
            f'  state_key_prefix = "terraform/azure/"\n'
        )

    @classmethod
    def _from_toml(
        cls,
        path: pathlib.Path,
        *,
        table: tuple[str, ...],
        project_root: pathlib.Path,
    ) -> "Config":
        with path.open("rb") as fin:
            data = tomllib.load(fin)
        section = _walk(data, table)
        if section is None:
            raise ConfigError(f"Missing [{'.'.join(table)}] table in {path}")
        return cls._build(section, project_root=project_root)

    @classmethod
    def _build(cls, raw: dict[str, Any], *, project_root: pathlib.Path) -> "Config":
        if not isinstance(raw, dict):
            raise ConfigError("tf_project config table must be a TOML table")

        def _path(key: str, default: str) -> pathlib.Path:
            value = raw.get(key, default)
            if not isinstance(value, str):
                raise ConfigError(f"`{key}` must be a string path, got {type(value).__name__}")
            p = pathlib.Path(value)
            if not p.is_absolute():
                p = project_root / p
            return p.resolve()

        state_key_prefix = raw.get("state_key_prefix", "")
        if not isinstance(state_key_prefix, str):
            raise ConfigError("`state_key_prefix` must be a string")

        state_file_name = raw.get("state_file_name", DEFAULT_STATE_FILE_NAME)
        tfplan_name = raw.get("tfplan_name", DEFAULT_TFPLAN_NAME)
        if not isinstance(state_file_name, str) or not isinstance(tfplan_name, str):
            raise ConfigError("`state_file_name` and `tfplan_name` must be strings")

        tmp_dir = _path("tmp_dir", DEFAULT_TMP_DIR)

        secrets_raw = raw.get("secrets", {})
        if not isinstance(secrets_raw, dict):
            raise ConfigError("[tf_project.secrets] must be a table")
        command_raw = secrets_raw.get("command", list(DEFAULT_SECRETS_COMMAND))
        if not isinstance(command_raw, list) or not all(isinstance(x, str) for x in command_raw):
            raise ConfigError("[tf_project.secrets].command must be a list of strings")

        return cls(
            project_root=project_root.resolve(),
            terraform_dir=_path("terraform_dir", DEFAULT_TERRAFORM_DIR),
            tfvars_dir=_path("tfvars_dir", DEFAULT_TFVARS_DIR),
            tmp_dir=tmp_dir,
            state_key_prefix=state_key_prefix,
            state_file=tmp_dir / state_file_name,
            tfplan_file=tmp_dir / tfplan_name,
            terraform_binary=_resolve_terraform_binary(raw.get("terraform_binary"), project_root=project_root),
            secrets=SecretsConfig(command=tuple(command_raw)),
        )


def _resolve_terraform_binary(value: Any, *, project_root: pathlib.Path) -> str:
    """Decide which terraform binary to invoke.

    - Unset → `shutil.which("terraform")` if available, else fall back to the
      bare name so the eventual subprocess call surfaces a clear error.
    - Value containing a path separator → resolved relative to the project
      root if not already absolute.
    - Bare name (e.g. `"tofu"`) → kept as-is so subprocess does its own PATH
      lookup at exec time.
    """
    if value is None:
        return shutil.which(DEFAULT_TERRAFORM_BINARY) or DEFAULT_TERRAFORM_BINARY
    if not isinstance(value, str) or not value:
        raise ConfigError("`terraform_binary` must be a non-empty string")
    if "/" in value or "\\" in value:
        p = pathlib.Path(value)
        if not p.is_absolute():
            p = (project_root / p).resolve()
        return str(p)
    return value


def _walk(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    cursor: Any = data
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor if isinstance(cursor, dict) else None
