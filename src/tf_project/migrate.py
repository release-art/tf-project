"""One-shot migration of pre-slot state into the slot-based layout.

The legacy layout had a single ``tmp/my_terraform_state.json`` with no
``schema_version`` field. The new layout shards state per slot under
``tmp/slots/<slot>/state.json`` (v1) and points to the active slot via
``tmp/active``.

`run_if_needed` is idempotent and best-effort: if the legacy file is
unreadable or references a missing tfvars, it logs a one-line warning,
removes the legacy file, and leaves the user to run ``tfp use <slot>``.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

from tf_project import banner as banner_mod
from tf_project.config import Config
from tf_project.models import MyState, ValueModel
from tf_project.state import write_active

LEGACY_STATE_FILENAME = "my_terraform_state.json"
LEGACY_TFPLAN_FILENAME = "my.tfplan"


class MigrationResult(ValueModel):
    """Outcome of `run_if_needed`."""

    migrated: bool
    slot: str | None = None
    breadcrumb: pathlib.Path | None = None
    note: str | None = None


def run_if_needed(config: Config) -> MigrationResult:
    """Migrate pre-slot state if present. Idempotent and safe to call on every run."""
    legacy_state = config.tmp_dir / LEGACY_STATE_FILENAME
    if not legacy_state.exists():
        return MigrationResult(migrated=False)

    try:
        raw = json.loads(legacy_state.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _abort(config, legacy_state, f"unreadable legacy state ({exc}); removing")

    tfvars_str = raw.get("tfvars") if isinstance(raw, dict) else None
    if not isinstance(tfvars_str, str):
        return _abort(config, legacy_state, "legacy state missing `tfvars`; removing")

    tfvars_path = pathlib.Path(tfvars_str)
    if not tfvars_path.exists():
        return _abort(
            config,
            legacy_state,
            f"legacy state references missing tfvars {tfvars_path}; "
            "removing legacy file — run `tfp use <slot>` to start fresh",
        )

    try:
        banner = banner_mod.find_banner(tfvars_path)
    except (banner_mod.ProjectInfoNotFoundError, banner_mod.BannerError) as exc:
        return _abort(config, legacy_state, f"banner unreadable in {tfvars_path} ({exc}); removing legacy file")

    slot_name, _ = banner_mod.resolve_slot_name(banner, tfvars=tfvars_path)
    source_root = (config.terraform_dir / banner.project).resolve()
    backend_config = banner_mod.resolve_backend_config(banner, tfvars=tfvars_path, config=config)

    # Refuse if a slot dir already exists with content — a partial earlier
    # upgrade or manual setup. Don't clobber.
    slot_dir = config.slot_dir_for(slot_name)
    if slot_dir.exists() and any(slot_dir.iterdir()):
        return _abort(
            config,
            legacy_state,
            f"slot dir {slot_dir} already exists and is non-empty; "
            "refusing to overwrite. Remove it manually then re-run.",
        )
    slot_dir.mkdir(parents=True, exist_ok=True)

    # Sanitise the legacy environ — older versions may have stashed
    # non-string values that pydantic's `dict[str, str]` will reject.
    raw_environ = raw.get("environ") or {}
    if not isinstance(raw_environ, dict):
        return _abort(config, legacy_state, f"legacy `environ` is not an object ({type(raw_environ).__name__})")
    environ: dict[str, str] = {}
    for k, v in raw_environ.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return _abort(
                config,
                legacy_state,
                f"legacy `environ` contains non-string entry {k!r}={v!r}",
            )
        environ[k] = v

    try:
        state = MyState(
            slot=slot_name,
            project=banner.project,
            tfvars=tfvars_path.resolve(),
            source_root=source_root,
            tfplan_location=config.tfplan_file_for(slot_name),
            environ=environ,
            backend_config=backend_config,
        )
    except Exception as exc:  # noqa: BLE001 — surface any construction failure cleanly
        return _abort(config, legacy_state, f"could not construct MyState from legacy ({exc})")
    state.dump_file(config.state_file_for(slot_name))

    # Move legacy tfplan + meta into the slot if they exist alongside.
    legacy_plan = config.tmp_dir / LEGACY_TFPLAN_FILENAME
    if legacy_plan.exists():
        legacy_plan.replace(config.tfplan_file_for(slot_name))
    legacy_meta = config.tmp_dir / (LEGACY_TFPLAN_FILENAME + ".meta.json")
    if legacy_meta.exists():
        legacy_meta.unlink()  # old shape, drop it — apply-safety re-derives on next plan

    write_active(config, slot_name)
    legacy_state.unlink(missing_ok=True)
    breadcrumb = _write_breadcrumb(config, f"migrated legacy state → slot {slot_name!r}")

    return MigrationResult(migrated=True, slot=slot_name, breadcrumb=breadcrumb)


def _abort(config: Config, legacy_state: pathlib.Path, note: str) -> MigrationResult:
    print(f"tf-project migration: {note}", file=sys.stderr)
    legacy_state.unlink(missing_ok=True)
    breadcrumb = _write_breadcrumb(config, f"aborted legacy migration: {note}")
    return MigrationResult(migrated=False, breadcrumb=breadcrumb, note=note)


def _write_breadcrumb(config: Config, text: str) -> pathlib.Path:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = config.tmp_dir / f"MIGRATED-{ts}.txt"
    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")
    return path


__all__ = ["MigrationResult", "run_if_needed"]
