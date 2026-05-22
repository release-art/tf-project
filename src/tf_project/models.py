"""Pydantic models for everything that crosses the disk boundary.

`VersionedModel` is the base for files carrying an explicit `schema_version`
field. Each subclass declares its `SUPPORTED_VERSION` class var and (optionally)
overrides `migrate` to transform older payloads forward.

In-memory value objects (resolver results, decisions) also live here when they
benefit from pydantic validation, but they subclass `ValueModel` instead — same
strictness, no schema versioning.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator


class SchemaError(RuntimeError):
    """A versioned file failed structural validation (missing/invalid version)."""


class SchemaIncompatibleError(SchemaError):
    """A versioned file was written by a newer tfp than this one supports."""


class PersistedModel(BaseModel):
    """Base for models that get serialised to JSON files on disk.

    Strict by default: unknown fields raise. Frozen so loaded objects can't
    drift away from what's on disk.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ValueModel(BaseModel):
    """Base for in-memory value objects (resolver results, decisions).

    Same strictness as `PersistedModel` with `arbitrary_types_allowed=True`
    so fields can hold things like `pathlib.Path` subclasses without extra
    wiring.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


class VersionedModel(PersistedModel):
    """A persisted model that carries an explicit `schema_version` integer.

    Subclasses set ``SUPPORTED_VERSION`` and may override ``migrate`` to walk
    older payloads forward into the current shape before validation.
    """

    SUPPORTED_VERSION: ClassVar[int]

    schema_version: int

    @classmethod
    def load_file(cls, path: pathlib.Path) -> Self:
        """Read and validate a versioned JSON file at `path`."""
        with path.open("rb") as fin:
            raw = json.load(fin)
        if not isinstance(raw, dict):
            raise SchemaError(f"{path}: expected a JSON object at the top level, got {type(raw).__name__}")
        found = raw.get("schema_version")
        if not isinstance(found, int):
            raise SchemaError(f"{path}: missing or non-int `schema_version`")
        if found > cls.SUPPORTED_VERSION:
            raise SchemaIncompatibleError(
                f"{path}: schema_version={found} was written by a newer tf-project; "
                f"this binary supports up to v{cls.SUPPORTED_VERSION}. Upgrade tfp."
            )
        if found < cls.SUPPORTED_VERSION:
            raw = cls.migrate(raw, from_version=found)
        return cls.model_validate(raw)

    def dump_file(self, path: pathlib.Path) -> None:
        """Atomically write the model to `path` as pretty-printed JSON.

        Writes to a sibling temp file in the same directory, then `os.replace`s
        over `path` — guarantees readers either see the old content or the
        new, never a half-written file (even on SIGINT mid-write).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(self.model_dump_json(indent=4) + "\n")
        tmp.replace(path)

    @classmethod
    def migrate(cls, raw: dict[str, Any], *, from_version: int) -> dict[str, Any]:
        """Walk a payload from `from_version` up to `SUPPORTED_VERSION`.

        Default: identity. Subclasses with breaking changes override this.
        """
        return raw


# ---- Banner -------------------------------------------------------------------


class Banner(BaseModel):
    """User-authored tfvars banner.

    Carved out of the strict default: `extra="allow"` so unknown fields are
    retained on `model_extra`, then a validator logs them to stderr. This
    keeps banner files forward-compatible (older tfp tolerates new banner
    fields) while still surfacing surprise content rather than silently
    swallowing it.

    Provenance (`tfvars` path) is passed via `model_validate(..., context=...)`
    so the model itself stays free of bookkeeping fields.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    header: Literal["terraform"]
    project: str = Field(min_length=1)
    slot: str | None = Field(default=None, min_length=1)
    state_key: str | None = Field(default=None, min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    backend_config: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _warn_on_extras(self, info: ValidationInfo) -> Self:
        if not self.model_extra:
            return self
        ctx = info.context or {}
        source = ctx.get("tfvars", "<unknown>")
        unknown = ", ".join(sorted(self.model_extra))
        print(f"warning: banner in {source} has unknown fields: {unknown}", file=sys.stderr)
        return self


# ---- MyState (per-slot persistent init state) ---------------------------------


class MyState(VersionedModel):
    """Persistent per-slot init state written under `slots/<slot>/state.json`."""

    SUPPORTED_VERSION: ClassVar[int] = 1
    schema_version: int = 1

    slot: str = Field(min_length=1)
    project: str = Field(min_length=1)
    tfvars: pathlib.Path
    source_root: pathlib.Path
    tfplan_location: pathlib.Path
    environ: dict[str, str] = Field(default_factory=dict)
    backend_config: dict[str, str] = Field(default_factory=dict)


# ---- TfplanMeta (apply-safety sidecar) ----------------------------------------


class TfplanMeta(VersionedModel):
    """Sidecar JSON next to a saved tfplan; locks apply to the matching inputs.

    `inputs_sha256` is the rolling hash over the decrypted tfvars and every
    `.tf` under source_root (so module edits invalidate the plan too).
    `project`, `state_key`, `backend_config`, `env` snapshot the
    banner-derived targeting at plan-time — banner-only edits that change
    the apply target invalidate the plan even if tfvars/.tf are unchanged.
    """

    SUPPORTED_VERSION: ClassVar[int] = 1
    schema_version: int = 1

    inputs_sha256: str
    project: str
    state_key: str | None = None
    backend_config: dict[str, str] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)


# ---- LastInvocation (tfp last) ------------------------------------------------


class LastInvocation(VersionedModel):
    """Last terraform invocation recorded by `tfp`, surfaced via `tfp last`."""

    SUPPORTED_VERSION: ClassVar[int] = 1
    schema_version: int = 1

    timestamp: str
    argv: list[str]
    exit_code: int | None = None


__all__ = [
    "Banner",
    "LastInvocation",
    "MyState",
    "PersistedModel",
    "SchemaError",
    "SchemaIncompatibleError",
    "TfplanMeta",
    "ValueModel",
    "VersionedModel",
]
