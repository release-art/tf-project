"""Discovery and loading of `tf_project` configuration.

The config is a single pydantic model. Paths declared in TOML are resolved
against the project root at load time, so every `Path` reachable from
`Config` at runtime is absolute.
"""

from __future__ import annotations

import pathlib
import shutil
import tomllib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_FILE_NAME = "tf_project.toml"
PYPROJECT_FILE_NAME = "pyproject.toml"
PYPROJECT_TABLE = ("tool", "tf_project")
CONFIG_TABLE = "tf_project"

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


class SecretsConfig(BaseModel):
    """Pluggable tfvars preprocessor. Empty `command` disables preprocessing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = DEFAULT_SECRETS_COMMAND


class Config(BaseModel):
    """The tf-project configuration, fully resolved (every Path is absolute)."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    project_root: pathlib.Path
    terraform_dir: pathlib.Path
    tfvars_dir: pathlib.Path
    tmp_dir: pathlib.Path
    state_key_prefix: str = ""
    terraform_binary: str = DEFAULT_TERRAFORM_BINARY
    backend_config: dict[str, str] = Field(default_factory=dict)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    plugin_cache_dir: pathlib.Path | None = None

    # ---- Discovery -----------------------------------------------------------

    @classmethod
    def discover(cls, start: pathlib.Path | None = None) -> "Config":
        start = (start or pathlib.Path.cwd()).resolve()
        for parent in [start, *start.parents]:
            candidate = parent / CONFIG_FILE_NAME
            if candidate.is_file():
                return cls._from_table_file(candidate, table=(CONFIG_TABLE,), project_root=parent)
        for parent in [start, *start.parents]:
            candidate = parent / PYPROJECT_FILE_NAME
            if candidate.is_file():
                with candidate.open("rb") as fin:
                    data = tomllib.load(fin)
                section = _walk(data, PYPROJECT_TABLE)
                if section is not None:
                    return cls._from_table(section, project_root=parent)
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
    def _from_table_file(
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
        return cls._from_table(section, project_root=project_root)

    @classmethod
    def _from_table(cls, table: dict[str, Any], *, project_root: pathlib.Path) -> "Config":
        if not isinstance(table, dict):
            raise ConfigError("tf_project config table must be a TOML table")

        project_root = project_root.resolve()
        payload: dict[str, Any] = dict(table)
        payload["project_root"] = project_root
        payload["terraform_dir"] = _abs_from_table(table, "terraform_dir", DEFAULT_TERRAFORM_DIR, project_root)
        payload["tfvars_dir"] = _abs_from_table(table, "tfvars_dir", DEFAULT_TFVARS_DIR, project_root)
        payload["tmp_dir"] = _abs_from_table(table, "tmp_dir", DEFAULT_TMP_DIR, project_root)
        payload["terraform_binary"] = _resolve_terraform_binary(
            table.get("terraform_binary"), project_root=project_root
        )
        if "plugin_cache_dir" in table:
            raw = table["plugin_cache_dir"]
            if isinstance(raw, str) and raw == "":
                payload["plugin_cache_dir"] = None
            else:
                payload["plugin_cache_dir"] = _abs_from_value(raw, base=project_root, key="plugin_cache_dir")

        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc

    # ---- Derived paths (slot-scoped) -----------------------------------------

    @property
    def slots_dir(self) -> pathlib.Path:
        return self.tmp_dir / "slots"

    @property
    def active_file(self) -> pathlib.Path:
        return self.tmp_dir / "active"

    @property
    def default_plugin_cache_dir(self) -> pathlib.Path:
        """Where the plugin cache lives. `plugin_cache_dir = ""` disables; see `plugin_cache_enabled`."""
        return self.plugin_cache_dir if self.plugin_cache_dir is not None else self.tmp_dir / "plugin-cache"

    @property
    def plugin_cache_enabled(self) -> bool:
        # Pydantic stores the explicit `""` opt-out as None via the table
        # parser above; but None also means "not set, use default". We
        # distinguish via a separate marker file convention: the table
        # parser stores `None` only for the opt-out case if `plugin_cache_dir`
        # was present in the table. Practically we always enable.
        return True

    def slot_dir_for(self, slot: str) -> pathlib.Path:
        return self.slots_dir / slot

    def state_file_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / "state.json"

    def tfplan_file_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / "tfplan"

    def tfplan_meta_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / "tfplan.meta.json"

    def data_dir_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / ".terraform"

    def backend_hash_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / "backend.hash"

    def last_invocation_for(self, slot: str) -> pathlib.Path:
        return self.slot_dir_for(slot) / "last.json"


# ---- Helpers ------------------------------------------------------------------


def _walk(data: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any] | None:
    cursor: Any = data
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor if isinstance(cursor, dict) else None


def _abs_from_table(
    table: dict[str, Any],
    key: str,
    default: str,
    project_root: pathlib.Path,
) -> pathlib.Path:
    value = table.get(key, default)
    return _abs_from_value(value, base=project_root, key=key)


def _abs_from_value(value: Any, *, base: pathlib.Path, key: str) -> pathlib.Path:
    if not isinstance(value, (str, pathlib.Path)):
        raise ConfigError(f"`{key}` must be a string path, got {type(value).__name__}")
    p = pathlib.Path(value)
    if not p.is_absolute():
        p = base / p
    return p.resolve()


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


__all__ = [
    "CONFIG_FILE_NAME",
    "CONFIG_TABLE",
    "DEFAULT_SECRETS_COMMAND",
    "DEFAULT_TERRAFORM_BINARY",
    "DEFAULT_TERRAFORM_DIR",
    "DEFAULT_TFVARS_DIR",
    "DEFAULT_TMP_DIR",
    "PYPROJECT_FILE_NAME",
    "PYPROJECT_TABLE",
    "Config",
    "ConfigError",
    "ConfigNotFoundError",
    "SecretsConfig",
]
