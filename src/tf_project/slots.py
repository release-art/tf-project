"""Slot resolution: banner → slot identity, uniqueness, active selection.

A slot is a named, persistent context bound to one tfvars file. Slot names
default to the tfvars file stem and can be overridden via the banner's
optional `slot` field. Each slot owns its own `TF_DATA_DIR` so flipping
between slots is a pointer write + env flip, not a `terraform init`.
"""

from __future__ import annotations

import os
import pathlib
from typing import Literal

from pydantic import ConfigDict, computed_field

from tf_project.banner import (
    BannerError,
    ProjectInfoNotFoundError,
    find_banner,
    resolve_backend_config,
    resolve_slot_name,
)
from tf_project.config import Config
from tf_project.models import Banner, ValueModel
from tf_project.state import read_active

ENV_VAR = "TFP_SLOT"


class SlotError(RuntimeError):
    """A slot-resolution failure surfaced to the CLI."""


class SlotInUseError(SlotError):
    """`tfp rm <slot>` blocked because the slot is active."""


class SlotNotFoundError(SlotError):
    """`tfp rm <slot>` could not find the slot directory on disk."""


class SlotConflictError(SlotError):
    """Two tfvars files claim the same slot name."""

    def __init__(self, a: "ResolvedSlot", b: "ResolvedSlot") -> None:
        self.a = a
        self.b = b
        a_kind = "explicit" if a.name_is_explicit else "default"
        b_kind = "explicit" if b.name_is_explicit else "default"
        super().__init__(
            f"slot {a.name!r} is defined by 2 tfvars files:\n"
            f"  {a.tfvars}      ({a_kind})\n"
            f"  {b.tfvars}      ({b_kind})\n"
            'Set an explicit "slot" in one banner, or rename the file.'
        )


class ResolvedSlot(ValueModel):
    """Everything needed to operate on a slot, derived from its banner.

    Constructed via `resolve_slot_from_banner` or `resolve_slot` (which goes
    through banner lookup first). All paths are absolute.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    name: str
    name_is_explicit: bool
    tfvars: pathlib.Path
    project: str
    source_root: pathlib.Path
    env: dict[str, str]
    backend_config: dict[str, str]
    slot_dir: pathlib.Path

    @computed_field
    @property
    def data_dir(self) -> pathlib.Path:
        return self.slot_dir / ".terraform"

    @computed_field
    @property
    def state_file(self) -> pathlib.Path:
        return self.slot_dir / "state.json"

    @computed_field
    @property
    def tfplan_file(self) -> pathlib.Path:
        return self.slot_dir / "tfplan"

    @computed_field
    @property
    def tfplan_meta_file(self) -> pathlib.Path:
        return self.slot_dir / "tfplan.meta.json"

    @computed_field
    @property
    def backend_hash_file(self) -> pathlib.Path:
        return self.slot_dir / "backend.hash"


class OrphanSlot(ValueModel):
    """A directory under `slots/` with no matching tfvars banner."""

    name: str
    slot_dir: pathlib.Path


class ActiveSlot(ValueModel):
    """The resolved active slot plus where the name came from."""

    name: str
    source: Literal["flag", "env", "file"]


# ---- Public API ---------------------------------------------------------------


def resolve_slot_from_banner(banner: Banner, tfvars: pathlib.Path, config: Config) -> ResolvedSlot:
    """Build a ResolvedSlot from an already-parsed banner."""
    name, explicit = resolve_slot_name(banner, tfvars=tfvars)
    return ResolvedSlot(
        name=name,
        name_is_explicit=explicit,
        tfvars=tfvars.resolve(),
        project=banner.project,
        source_root=(config.terraform_dir / banner.project).resolve(),
        env=dict(banner.env),
        backend_config=resolve_backend_config(banner, tfvars=tfvars, config=config),
        slot_dir=config.slot_dir_for(name),
    )


def resolve_slot(arg: str, *, config: Config) -> ResolvedSlot:
    """Resolve a user-supplied slot identifier.

    `arg` is treated as a path if it contains a path separator or ends in
    `.tfvars`; otherwise as a slot name to look up across `tfvars_dir`.
    """
    if looks_like_path(arg):
        path = pathlib.Path(arg)
        if not path.is_absolute():
            path = (pathlib.Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            raise SlotError(f"tfvars file not found: {arg}")
        banner = find_banner(path)
        return resolve_slot_from_banner(banner, path, config)

    for slot in list_slots(config):
        if slot.name == arg:
            return slot
    available = ", ".join(sorted({s.name for s in list_slots(config)})) or "(none)"
    raise SlotError(f"no tfvars defines slot {arg!r}. Available: {available}")


def list_slots(config: Config) -> list[ResolvedSlot]:
    """All slots derived from `tfvars_dir/*.tfvars` banners, sorted by name.

    Files without a banner or with a malformed banner are skipped silently -
    `tfp self banner check` is the diagnostic surface for those.
    """
    if not config.tfvars_dir.is_dir():
        return []
    out: list[ResolvedSlot] = []
    for path in sorted(config.tfvars_dir.rglob("*.tfvars")):
        try:
            banner = find_banner(path)
        except (ProjectInfoNotFoundError, BannerError):
            continue
        out.append(resolve_slot_from_banner(banner, path.resolve(), config))
    out.sort(key=lambda s: s.name)
    return out


def validate_unique(slots: list[ResolvedSlot]) -> None:
    """Raise `SlotConflictError` if any two slots share a name."""
    seen: dict[str, ResolvedSlot] = {}
    for slot in slots:
        if slot.name in seen:
            raise SlotConflictError(seen[slot.name], slot)
        seen[slot.name] = slot


def list_orphans(config: Config) -> list[OrphanSlot]:
    """Slot directories under `slots/` with no matching banner."""
    if not config.slots_dir.is_dir():
        return []
    valid_names = {s.name for s in list_slots(config)}
    out: list[OrphanSlot] = []
    for child in sorted(config.slots_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name in valid_names:
            continue
        out.append(OrphanSlot(name=child.name, slot_dir=child.resolve()))
    return out


def resolve_active(config: Config, *, flag: str | None = None) -> ActiveSlot | None:
    """Resolve the active slot: -s flag > TFP_SLOT env > tmp/active file.

    Returns None if none of the three sources yield a slot name. A name
    that doesn't correspond to any tfvars banner is *still* returned (so
    `tfp use <new-slot>` can target a not-yet-resolvable slot via path),
    but `assert_slot_exists` is the recommended caller-side check before
    operating on a flag-provided slot.
    """
    if flag:
        return ActiveSlot(name=flag, source="flag")
    env = os.environ.get(ENV_VAR)
    if env:
        return ActiveSlot(name=env, source="env")
    saved = read_active(config)
    if saved:
        return ActiveSlot(name=saved, source="file")
    return None


def assert_slot_exists(config: Config, name: str) -> None:
    """Raise `SlotError` if `name` is not defined by any tfvars banner."""
    known = {s.name for s in list_slots(config)}
    if name not in known:
        available = ", ".join(sorted(known)) or "(none)"
        raise SlotError(f"no tfvars defines slot {name!r}. Available: {available}")


def looks_like_path(arg: str) -> bool:
    """True if `arg` should be treated as a tfvars path, not a slot name.

    Heuristic: ends in `.tfvars` or contains a path separator. Anything else
    is interpreted as a slot name and looked up via `list_slots`.
    """
    return arg.endswith(".tfvars") or "/" in arg or "\\" in arg


# Re-exports for convenience
__all__ = [
    "ENV_VAR",
    "ActiveSlot",
    "OrphanSlot",
    "ResolvedSlot",
    "SlotConflictError",
    "SlotError",
    "SlotInUseError",
    "SlotNotFoundError",
    "assert_slot_exists",
    "list_orphans",
    "list_slots",
    "resolve_active",
    "resolve_slot",
    "resolve_slot_from_banner",
    "validate_unique",
]
