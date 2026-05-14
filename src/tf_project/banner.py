"""Tfvars banner parsing and validation.

A banner is a single-line JSON object embedded in a `#` comment at the top
of a tfvars file, e.g.:

    # {"header":"terraform","project":"api","state_key":"shared/api.tfstate"}

The `header` field must equal `"terraform"` for the line to be recognised.
Other supported fields are validated lazily by the per-field parsers below.
"""

from __future__ import annotations

import json
import pathlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tf_project.config import Config


class ProjectInfoNotFoundError(RuntimeError):
    """No `# {...}` JSON banner with `header == "terraform"` was found."""


class BannerError(ValueError):
    """A field inside a terraform banner failed validation."""

    def __init__(self, tfvars: pathlib.Path, message: str) -> None:
        self.tfvars = tfvars
        super().__init__(f"banner in {tfvars}: {message}")


def find_project_info(tfvars: pathlib.Path) -> dict[str, Any]:
    """Locate the `# {...}` banner line; return its parsed JSON object."""
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


def parse_project(info: dict[str, Any], *, tfvars: pathlib.Path) -> str:
    name = info.get("project")
    if not isinstance(name, str) or not name:
        raise BannerError(tfvars, "`project` must be a non-empty string")
    return name


def parse_env(info: dict[str, Any], *, tfvars: pathlib.Path) -> dict[str, str]:
    raw = info.get("env")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BannerError(tfvars, "`env` must be a JSON object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise BannerError(tfvars, f"`env` must map string keys to string values; got {key!r}={value!r}")
        out[key] = value
    return out


def parse_backend_config(info: dict[str, Any], *, tfvars: pathlib.Path) -> dict[str, str]:
    raw = info.get("backend_config")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise BannerError(tfvars, "`backend_config` must be a JSON object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise BannerError(
                tfvars,
                f"`backend_config` must map string keys to string values; got {key!r}={value!r}",
            )
        out[key] = value
    return out


def _parse_state_key(info: dict[str, Any], *, tfvars: pathlib.Path) -> str | None:
    value = info.get("state_key")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BannerError(tfvars, "`state_key` must be a non-empty string")
    return value


def resolve_backend_config(
    info: dict[str, Any],
    *,
    tfvars: pathlib.Path,
    config: "Config",
) -> dict[str, str]:
    """Compute the final `-backend-config` k/v map for this tfvars.

    Precedence (later wins):
      1. Synthesised `key = <state_key_prefix><tfvars-stem>.tfstate`
         (only if `state_key_prefix` is set).
      2. `[tf_project.backend_config]` from config.
      3. `backend_config` table from the banner.
      4. `state_key` from the banner — final word on `key`.
    """
    merged: dict[str, str] = {}
    if config.state_key_prefix:
        merged["key"] = f"{config.state_key_prefix}{tfvars.stem}.tfstate"
    merged.update(config.backend_config)
    merged.update(parse_backend_config(info, tfvars=tfvars))
    state_key = _parse_state_key(info, tfvars=tfvars)
    if state_key is not None:
        merged["key"] = state_key
    return merged


def render_summary(
    info: dict[str, Any],
    *,
    tfvars: pathlib.Path,
    config: "Config",
) -> dict[str, Any]:
    """Validate every banner field and return a structured summary."""
    project = parse_project(info, tfvars=tfvars)
    return {
        "tfvars": str(tfvars),
        "project": project,
        "source_root": str(config.terraform_dir / project),
        "backend_config": resolve_backend_config(info, tfvars=tfvars, config=config),
        "env": parse_env(info, tfvars=tfvars),
    }
