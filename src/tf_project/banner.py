"""Tfvars banner location and parsing.

A banner is a single-line JSON object embedded in a `#` comment at the top
of a tfvars file:

    # {"header":"terraform","project":"api","state_key":"shared/api.tfstate"}

The `header` field must equal `"terraform"`. The pydantic `Banner` model
(in `tf_project.models`) handles structural validation; this module is the
file-scanning + error-translation layer that sits on top of it.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import TYPE_CHECKING

from pydantic import ValidationError

from tf_project.models import Banner

if TYPE_CHECKING:
    from tf_project.config import Config


class ProjectInfoNotFoundError(RuntimeError):
    """No `# {...}` JSON banner with `header == "terraform"` was found."""


class BannerError(ValueError):
    """A banner failed validation."""

    def __init__(self, tfvars: pathlib.Path, message: str) -> None:
        self.tfvars = tfvars
        super().__init__(f"banner in {tfvars}: {message}")


def find_banner(tfvars: pathlib.Path) -> Banner:
    """Locate and parse the banner in `tfvars`.

    Raises `ProjectInfoNotFoundError` if no banner line is present, and
    `BannerError` if the banner is malformed.
    """
    with tfvars.open("r") as fin:
        for line in fin:
            if not line.startswith("#"):
                continue
            maybe_json = line.lstrip("#").strip()
            try:
                data = json.loads(maybe_json)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or data.get("header") != "terraform":
                continue
            try:
                parsed = Banner.model_validate(data)
            except ValidationError as exc:
                raise BannerError(tfvars, _format_validation_error(exc)) from exc
            if parsed.model_extra:
                unknown = ", ".join(sorted(parsed.model_extra))
                print(f"warning: banner in {tfvars} has unknown fields: {unknown}", file=sys.stderr)
            return parsed
    raise ProjectInfoNotFoundError(
        f"Project info banner not found in {tfvars}. "
        'Expected a comment line like `# {"header": "terraform", "project": "<name>"}`.'
    )


def resolve_backend_config(
    banner: Banner,
    *,
    tfvars: pathlib.Path,
    config: "Config",
) -> dict[str, str]:
    """Compute the final `-backend-config` k/v map for this banner.

    Precedence (later wins):
      1. Synthesised `key = <state_key_prefix><tfvars-stem>.tfstate`
         (only if `state_key_prefix` is set).
      2. `[tf_project.backend_config]` from config.
      3. `backend_config` from the banner.
      4. `state_key` from the banner — final word on `key`.
    """
    merged: dict[str, str] = {}
    if config.state_key_prefix:
        merged["key"] = f"{config.state_key_prefix}{tfvars.stem}.tfstate"
    merged.update(config.backend_config)
    merged.update(banner.backend_config)
    if banner.state_key is not None:
        merged["key"] = banner.state_key
    return merged


def resolve_slot_name(banner: Banner, *, tfvars: pathlib.Path) -> tuple[str, bool]:
    """Return (slot_name, is_explicit). Defaults to tfvars stem if banner has no `slot`."""
    if banner.slot is not None:
        return banner.slot, True
    return tfvars.stem, False


def render_summary(banner: Banner, *, tfvars: pathlib.Path, config: "Config") -> dict[str, object]:
    """Structured summary of a banner, used by `tfp self banner check`."""
    slot, explicit = resolve_slot_name(banner, tfvars=tfvars)
    return {
        "tfvars": str(tfvars),
        "project": banner.project,
        "slot": slot,
        "slot_is_explicit": explicit,
        "source_root": str(config.terraform_dir / banner.project),
        "backend_config": resolve_backend_config(banner, tfvars=tfvars, config=config),
        "env": dict(banner.env),
    }


def _format_validation_error(exc: ValidationError) -> str:
    """One-line user-readable summary of a pydantic ValidationError."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) if parts else str(exc)


__all__ = [
    "Banner",
    "BannerError",
    "ProjectInfoNotFoundError",
    "find_banner",
    "render_summary",
    "resolve_backend_config",
    "resolve_slot_name",
]
