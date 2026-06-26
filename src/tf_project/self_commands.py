"""Implementations behind `tfp self ...` subcommands."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import tomllib
from typing import Any, Literal, NamedTuple

from tf_project import (
    banner as banner_mod,
    lock as lock_mod,
    slots as slots_mod,
)
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
from tf_project.models import MyState
from tf_project.state import load_state


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

# Extra GOOS_GOARCH platforms to include when `tfp use` chases `terraform init`
# with `terraform providers lock`. linux_amd64, linux_arm64, and the host's
# own platform are always included; list anything else your team runs on here.
# extra_lock_platforms = ["windows_amd64", "darwin_amd64"]

# Max number of auto-snapshots kept per slot under <tmp_dir>/snapshots/<slot>/.
# Each `tfp state rm`/`mv`/`replace-provider`/`import` pulls remote state and
# writes a snapshot here before mutating. Set to 0 to disable.
# snapshot_retention = 200

[{_TABLE_PREFIX_PLACEHOLDER}.secrets]
# Pluggable tfvars preprocessor. Set `command = []` to disable.
command = {list(DEFAULT_SECRETS_COMMAND)!r}
"""


def _render_default_body(*, prefix: str) -> str:
    header = f"[{prefix}]\n"
    return header + DEFAULT_CONFIG_BODY.replace(_TABLE_PREFIX_PLACEHOLDER, prefix)


def do_self_init(cwd: pathlib.Path) -> pathlib.Path:
    """Bootstrap a config in `cwd`. Refuses to overwrite an existing config."""
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


def do_self_state_show(config: Config, *, slot: str | None, all_slots: bool) -> str:
    """Print saved-state JSON.

    Precedence:
      - ``all_slots=True``  → emit a map of {slot_name → state | null} for
        every slot derived from `tfvars_dir`. `slot` is ignored.
      - ``slot="name"``     → emit just that slot's state.
      - ``slot=None``       → use the active slot.
    """
    if all_slots:
        out: dict[str, Any] = {}
        for s in slots_mod.list_slots(config):
            st = load_state(config, s.name)
            out[s.name] = st.model_dump(mode="json") if st is not None else None
        return json.dumps(out, indent=2, sort_keys=True)

    name = _resolve_slot_name(config, slot=slot)
    state = load_state(config, name)
    if state is None:
        raise SelfCommandError(f"No state for slot {name!r} at {config.state_file_for(name)}.")
    return json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True)


def do_self_state_clear(config: Config, *, slot: str | None, all_slots: bool) -> list[pathlib.Path]:
    """Remove state files; return the paths that were removed.

    Same `all_slots` / `slot` precedence as :func:`do_self_state_show`.
    """
    removed: list[pathlib.Path] = []
    if all_slots:
        for s in slots_mod.list_slots(config):
            p = config.state_file_for(s.name)
            if p.exists():
                p.unlink()
                removed.append(p)
        return removed
    name = _resolve_slot_name(config, slot=slot)
    p = config.state_file_for(name)
    if p.exists():
        p.unlink()
        removed.append(p)
    return removed


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
        checks.append(DoctorCheck(name="secrets command", ok=True, detail="disabled (command = [])"))

    checks.append(_dir_check("terraform_dir", config.terraform_dir))
    checks.append(_dir_check("tfvars_dir", config.tfvars_dir))
    checks.append(_dir_check("tmp_dir", config.tmp_dir, allow_missing=True))
    checks.append(_dir_check("slots_dir", config.slots_dir, allow_missing=True))

    # Uniqueness check.
    try:
        slots_mod.validate_unique(slots_mod.list_slots(config))
        checks.append(DoctorCheck(name="slot uniqueness", ok=True, detail="all banners produce distinct slot names"))
    except slots_mod.SlotConflictError as exc:
        checks.append(DoctorCheck(name="slot uniqueness", ok=False, detail=str(exc)))

    # Orphan check.
    orphans = slots_mod.list_orphans(config)
    if orphans:
        checks.append(
            DoctorCheck(
                name="orphan slots",
                ok=False,
                detail=("found orphan slot directories with no matching banner: " + ", ".join(o.name for o in orphans)),
            )
        )
    else:
        checks.append(DoctorCheck(name="orphan slots", ok=True, detail="none"))

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
            detail=f"{path} (missing - will be created on demand)",
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
        "slots_dir": str(config.slots_dir),
        "active_file": str(config.active_file),
        "plugin_cache_dir": (
            str(config.effective_plugin_cache_dir) if config.effective_plugin_cache_dir is not None else "(disabled)"
        ),
        "state_key_prefix": config.state_key_prefix,
        "terraform_binary": config.terraform_binary,
        "backend_config": dict(config.backend_config),
        "secrets": {"command": list(config.secrets.command)},
        "extra_lock_platforms": list(config.extra_lock_platforms),
        "snapshot_retention": config.snapshot_retention,
    }


def do_self_banner_check(config: Config, *, tfvars: pathlib.Path) -> dict[str, Any]:
    """Parse a banner; return the resolved summary."""
    banner = banner_mod.find_banner(tfvars.resolve())
    return banner_mod.render_summary(banner, tfvars=tfvars.resolve(), config=config)


# ---- tfp self trace -----------------------------------------------------------

_TRACE_PLACEHOLDER = "<DECRYPTED_TFVARS>"

TraceSubcommand = Literal["use", "init", "plan", "apply", "refresh", "destroy", "output", "import"]
TRACE_SUBCOMMANDS: tuple[TraceSubcommand, ...] = (
    "use",
    "init",
    "plan",
    "apply",
    "refresh",
    "destroy",
    "output",
    "import",
)


def do_self_trace(
    config: Config,
    *,
    subcommand: str,
    slot: str | None = None,
    targets: list[str] | None = None,
    replaces: list[str] | None = None,
    extra: list[str] | None = None,
    import_address: str = "<RESOURCE_ADDRESS>",
    import_id: str = "<RESOURCE_ID>",
) -> list[str]:
    """Return the argv `tfp <subcommand>` would build, without invoking anything."""
    from tf_project import commands  # late import to avoid circular

    if subcommand not in TRACE_SUBCOMMANDS:
        raise SelfCommandError(f"Unknown subcommand {subcommand!r}. Choose from: {', '.join(TRACE_SUBCOMMANDS)}.")

    name = _resolve_slot_name(config, slot=slot)

    # `use` / `init` trace an init from the current banner - they don't read
    # saved state, so don't require it. Other subcommands need state because
    # their argv depends on saved tfvars / source_root / etc.
    if subcommand in ("use", "init"):
        try:
            resolved = slots_mod.resolve_slot(name, config=config)
        except slots_mod.SlotError as exc:
            raise SelfCommandError(str(exc)) from exc
        return commands.build_init_argv(config, resolved=resolved, extra=extra)

    state = load_state(config, name)
    if state is None:
        raise SelfCommandError(f"No state for slot {name!r}. Run `tfp use {name}` first.")

    var_file = _TRACE_PLACEHOLDER
    match subcommand:
        case "plan":
            return commands.build_plan_argv(
                config, state=state, var_file=var_file, targets=targets, replaces=replaces, extra=extra
            )
        case "apply":
            return commands.build_apply_argv(config, state=state, var_file=var_file, extra=extra)
        case "refresh":
            return commands.build_refresh_argv(config, state=state, var_file=var_file, targets=targets, extra=extra)
        case "destroy":
            return commands.build_destroy_argv(config, state=state, var_file=var_file, targets=targets, extra=extra)
        case "output":
            return commands.build_output_argv(config, state=state, extra=extra)
        case "import":
            return commands.build_import_argv(
                config,
                state=state,
                var_file=var_file,
                address=import_address,
                resource_id=import_id,
                extra=extra,
            )
        case _:  # pragma: no cover - already gated by TRACE_SUBCOMMANDS membership
            raise AssertionError(f"unhandled subcommand {subcommand!r}")


# ---- tfp last -----------------------------------------------------------------


def do_last_invocation(config: Config, *, slot: str | None = None) -> dict[str, Any]:
    name = _resolve_slot_name(config, slot=slot)
    path = config.last_invocation_for(name)
    if not path.exists():
        raise SelfCommandError(f"No prior invocation recorded at {path}.")
    from tf_project.models import LastInvocation

    last = LastInvocation.load_file(path)
    return last.model_dump(mode="json")


# ---- tfp self snapshot --------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class SnapshotEntry:
    path: pathlib.Path
    size: int
    mtime: float


def _utc_timestamp() -> str:
    """UTC timestamp with microsecond resolution so back-to-back snapshots don't collide."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")


def _slugify_label(label: str) -> str:
    """Restrict labels to safe filename characters."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in label)
    return cleaned.strip("-") or "snapshot"


def _pull_remote_state(config: Config, state: MyState) -> bytes:
    from tf_project import terraform as terraform_mod

    try:
        result = subprocess.run(  # noqa: S603 - explicit invocation
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "state",
                "pull",
            ],
            capture_output=True,
            check=True,
            env=terraform_mod.env_for_slot(config, state),
        )
    except FileNotFoundError as exc:
        raise SelfCommandError(f"{config.terraform_binary}: command not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr.decode("utf-8", "replace") or "").strip() or f"exit {exc.returncode}"
        raise SelfCommandError(f"terraform state pull failed: {detail}") from exc
    return result.stdout


def _prune_snapshots(snapshots_dir: pathlib.Path, *, keep: int) -> int:
    """Delete oldest snapshots so at most `keep` remain. Returns count pruned."""
    if keep <= 0 or not snapshots_dir.is_dir():
        return 0
    entries = sorted(
        (p for p in snapshots_dir.glob("*.tfstate") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    victims = entries[keep:]
    for victim in victims:
        try:
            victim.unlink()
        except OSError:
            # Best-effort: a snapshot we can't delete is annoying but not
            # worth failing the parent op for.
            continue
    return len(victims)


def _write_snapshot(config: Config, state: MyState, *, label: str) -> pathlib.Path:
    """Pull remote tfstate and write to the per-slot snapshots dir, pruning to retention."""
    dest_dir = config.snapshots_dir_for(state.slot)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_utc_timestamp()}-{_slugify_label(label)}.tfstate"
    dest.write_bytes(_pull_remote_state(config, state))
    _prune_snapshots(dest_dir, keep=config.snapshot_retention)
    return dest


def auto_snapshot(config: Config, state: MyState, *, op: str) -> pathlib.Path | None:
    """Auto-snapshot the remote tfstate before a state-mutating wrapped op.

    Returns the snapshot path, or None if disabled via
    `snapshot_retention = 0` in the config. Raises `SelfCommandError` if
    `terraform state pull` fails - by design, a missing safety net should
    abort the destructive op rather than silently proceed.
    """
    if config.snapshot_retention <= 0:
        return None
    return _write_snapshot(config, state, label=f"pre-{op}")


def do_self_snapshot(
    config: Config,
    *,
    slot: str | None = None,
    dest: pathlib.Path | None = None,
) -> pathlib.Path:
    """Pull the current remote tfstate to a local file via `terraform state pull`.

    With `dest=None` the snapshot is written into the per-slot snapshots dir
    and counts toward retention; with an explicit `dest` it goes wherever the
    caller asked and retention is not touched.
    """
    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    if dest is None:
        return _write_snapshot(config, state, label="manual")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_pull_remote_state(config, state))
    return dest


def do_self_snapshot_list(config: Config, *, slot: str | None = None) -> list[SnapshotEntry]:
    """Return snapshots for the slot, newest first."""
    name = _resolve_slot_name(config, slot=slot)
    _require_state_or_error(config, name)  # consistency with the other self-snapshot ops
    snapshots_dir = config.snapshots_dir_for(name)
    if not snapshots_dir.is_dir():
        return []
    entries = []
    for path in snapshots_dir.glob("*.tfstate"):
        if not path.is_file():
            continue
        stat = path.stat()
        entries.append(SnapshotEntry(path=path, size=stat.st_size, mtime=stat.st_mtime))
    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries


def do_self_snapshot_restore(
    config: Config,
    *,
    slot: str | None = None,
    name: str | None = None,
) -> pathlib.Path:
    """Push a saved snapshot back to the remote backend via `terraform state push -force`.

    `name` selects a specific snapshot (basename or stem inside the per-slot
    snapshots dir). With `name=None` the most recent snapshot is restored.
    Raises `SelfCommandError` if no snapshots exist or the named one is missing.
    """
    from tf_project import terraform as terraform_mod

    slot_name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, slot_name)
    entries = do_self_snapshot_list(config, slot=slot_name)
    if not entries:
        raise SelfCommandError(
            f"No snapshots to restore for slot {slot_name!r}. "
            f"Run `tfp self snapshot create` or perform a state-mutating op first."
        )
    if name is None:
        target = entries[0].path
    else:
        candidates = [e.path for e in entries if e.path.name == name or e.path.stem == name]
        if not candidates:
            raise SelfCommandError(
                f"No snapshot named {name!r} for slot {slot_name!r}. "
                f"Available: {', '.join(e.path.name for e in entries[:5])}" + (" ..." if len(entries) > 5 else "")
            )
        target = candidates[0]
    try:
        subprocess.run(  # noqa: S603 - explicit invocation
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "state",
                "push",
                "-force",
                str(target),
            ],
            check=True,
            env=terraform_mod.env_for_slot(config, state),
        )
    except FileNotFoundError as exc:
        raise SelfCommandError(f"{config.terraform_binary}: command not found") from exc
    except subprocess.CalledProcessError as exc:
        raise SelfCommandError(f"terraform state push failed (exit {exc.returncode})") from exc
    return target


# ---- tfp self providers lock --------------------------------------------------


def do_self_providers_lock(
    config: Config,
    *,
    slot: str | None = None,
    platforms: list[str] | None = None,
) -> list[str]:
    """Run `terraform providers lock` across the given platforms.

    Defaults to `linux_amd64` + `linux_arm64` plus the host's platform
    when called with `platforms=None`.
    """
    from tf_project import terraform as terraform_mod
    from tf_project.commands import build_providers_lock_argv

    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    argv = build_providers_lock_argv(
        config,
        source_root=state.source_root,
        platforms=platforms,
    )
    terraform_mod.run(argv, env=terraform_mod.env_for_slot(config, state))
    return argv


# ---- Remote-state lock helpers ------------------------------------------------


class LockStatus(NamedTuple):
    backend: str
    locked: bool
    detail: str
    lock_id: str | None = None
    lock_who: str | None = None
    lock_operation: str | None = None
    lock_created: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class BreakResult:
    method: str  # "force-unlock" | "lease-break"
    backend: str
    lock_id: str | None = None


def _require_state_or_error(config: Config, slot: str) -> MyState:
    """Adapter: delegate to commands._require_slot_state, re-raising as SelfCommandError."""
    from tf_project import commands

    try:
        return commands._require_slot_state(config, slot)
    except commands.StateNotInitializedError as exc:
        raise SelfCommandError(str(exc)) from exc


def _select_provider(state: MyState) -> lock_mod.LockProvider:
    try:
        return lock_mod.select_provider(state)
    except lock_mod.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc


def do_self_lock_status(config: Config, *, slot: str | None = None) -> LockStatus:
    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    provider = _select_provider(state)
    try:
        report = provider.status()
    except lock_mod.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc
    info = report.info
    return LockStatus(
        backend=report.backend,
        locked=report.locked,
        detail=report.detail,
        lock_id=info.id,
        lock_who=info.who,
        lock_operation=info.operation,
        lock_created=info.created,
    )


def do_self_lock_break(config: Config, *, slot: str | None = None, blunt: bool = False) -> BreakResult:
    from tf_project import terraform as terraform_mod

    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    provider = _select_provider(state)

    if not blunt:
        # Polite path: try terraform force-unlock first if we can discover a
        # lock ID. If status itself fails (e.g. CLI auth blip), fall through
        # to the blunt break - the user asked for the lock to go away.
        try:
            report = provider.status()
        except lock_mod.LockProviderError:
            report = None

        if report is not None:
            if report.info.id is None:
                # No lock to break. The user asked politely; don't escalate
                # to a backend-level lease break behind their back.
                raise SelfCommandError(
                    f"No lock to break on the {provider.backend} backend. "
                    "Pass --blunt only if you know a stale lease needs releasing."
                )
            try:
                terraform_mod.run(
                    [
                        config.terraform_binary,
                        f"-chdir={state.source_root}",
                        "force-unlock",
                        "-force",
                        report.info.id,
                    ],
                    env=terraform_mod.env_for_slot(config, state),
                )
            except terraform_mod.TerraformExit:
                # terraform itself failed - fall through to blunt break.
                pass
            else:
                return BreakResult(
                    method="force-unlock",
                    backend=provider.backend,
                    lock_id=report.info.id,
                )

    try:
        provider.break_lease()
    except lock_mod.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc
    return BreakResult(method="lease-break", backend=provider.backend)


# ---- helpers ------------------------------------------------------------------


def _resolve_slot_name(config: Config, *, slot: str | None) -> str:
    """Adapter: delegate to commands._require_active, re-raising as SelfCommandError."""
    from tf_project import commands

    try:
        return commands._require_active(config, flag=slot).name
    except commands.NoActiveSlotError as exc:
        raise SelfCommandError(str(exc)) from exc


__all__ = [
    "BreakResult",
    "DoctorCheck",
    "LockStatus",
    "SelfCommandError",
    "do_last_invocation",
    "do_self_banner_check",
    "do_self_config_path",
    "do_self_config_print",
    "do_self_doctor",
    "do_self_init",
    "do_self_lock_break",
    "do_self_lock_status",
    "SnapshotEntry",
    "auto_snapshot",
    "do_self_providers_lock",
    "do_self_snapshot",
    "do_self_snapshot_list",
    "do_self_snapshot_restore",
    "do_self_state_clear",
    "do_self_state_show",
    "do_self_trace",
]
