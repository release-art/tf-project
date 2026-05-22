"""Implementations behind `tfp self ...` subcommands."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import tomllib
from typing import Any, NamedTuple

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
    """Print state for the active slot (or `slot` override), or every slot when `all_slots`."""
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
    """Remove the state file for one or all slots; return the removed paths."""
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
                detail=(
                    "found orphan slot directories with no matching banner: "
                    + ", ".join(o.name for o in orphans)
                ),
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
        "slots_dir": str(config.slots_dir),
        "active_file": str(config.active_file),
        "plugin_cache_dir": str(config.default_plugin_cache_dir),
        "state_key_prefix": config.state_key_prefix,
        "terraform_binary": config.terraform_binary,
        "backend_config": dict(config.backend_config),
        "secrets": {"command": list(config.secrets.command)},
    }


def do_self_banner_check(config: Config, *, tfvars: pathlib.Path) -> dict[str, Any]:
    """Parse a banner; return the resolved summary."""
    banner = banner_mod.find_banner(tfvars.resolve())
    return banner_mod.render_summary(banner, tfvars=tfvars.resolve(), config=config)


# ---- tfp self trace -----------------------------------------------------------

_TRACE_PLACEHOLDER = "<DECRYPTED_TFVARS>"


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

    name = _resolve_slot_name(config, slot=slot)
    state = load_state(config, name)
    if state is None:
        raise SelfCommandError(f"No state for slot {name!r}. Run `tfp use {name}` first.")

    # For the `use`/`init` trace, we need a ResolvedSlot built from the
    # current banner.
    try:
        resolved = slots_mod.resolve_slot(name, config=config)
    except slots_mod.SlotError as exc:
        raise SelfCommandError(str(exc)) from exc

    builders = {
        "use": lambda: commands.build_init_argv(config, resolved=resolved, extra=extra),
        "plan": lambda: commands.build_plan_argv(
            config,
            state=state,
            var_file=_TRACE_PLACEHOLDER,
            targets=targets,
            replaces=replaces,
            extra=extra,
        ),
        "apply": lambda: commands.build_apply_argv(config, state=state, var_file=_TRACE_PLACEHOLDER, extra=extra),
        "refresh": lambda: commands.build_refresh_argv(
            config, state=state, var_file=_TRACE_PLACEHOLDER, targets=targets, extra=extra
        ),
        "destroy": lambda: commands.build_destroy_argv(
            config, state=state, var_file=_TRACE_PLACEHOLDER, targets=targets, extra=extra
        ),
        "output": lambda: commands.build_output_argv(config, state=state, extra=extra),
        "import": lambda: commands.build_import_argv(
            config,
            state=state,
            var_file=_TRACE_PLACEHOLDER,
            address=import_address,
            resource_id=import_id,
            extra=extra,
        ),
    }
    if subcommand not in builders:
        raise SelfCommandError(f"Unknown subcommand {subcommand!r}. Choose from: {', '.join(sorted(builders))}.")
    return builders[subcommand]()


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


def do_self_snapshot(
    config: Config,
    *,
    slot: str | None = None,
    dest: pathlib.Path | None = None,
) -> pathlib.Path:
    """Pull the current remote tfstate to a local file via `terraform state pull`."""
    import datetime

    from tf_project import terraform as terraform_mod

    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    if dest is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = config.tmp_dir / f"snapshot-{ts}.tfstate"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 — explicit invocation
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "state",
                "pull",
            ],
            capture_output=True,
            check=True,
            env=terraform_mod.merged_env(
                state.environ,
                data_dir=config.data_dir_for(state.slot),
                plugin_cache_dir=config.default_plugin_cache_dir,
            ),
        )
    except FileNotFoundError as exc:
        raise SelfCommandError(f"{config.terraform_binary}: command not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr.decode("utf-8", "replace") or "").strip() or f"exit {exc.returncode}"
        raise SelfCommandError(f"terraform state pull failed: {detail}") from exc
    dest.write_bytes(result.stdout)
    return dest


# ---- tfp self providers lock --------------------------------------------------


def do_self_providers_lock(
    config: Config,
    *,
    slot: str | None = None,
    platforms: list[str] | None = None,
) -> list[str]:
    """Run `terraform providers lock` across the given platforms.

    Recommended set (`linux_amd64`, `linux_arm64`, `darwin_amd64`,
    `darwin_arm64`) when called with `platforms=None`.
    """
    from tf_project import terraform as terraform_mod

    name = _resolve_slot_name(config, slot=slot)
    state = _require_state_or_error(config, name)
    platforms = platforms or [
        "linux_amd64",
        "linux_arm64",
        "darwin_amd64",
        "darwin_arm64",
    ]
    argv = [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "providers",
        "lock",
        *[f"-platform={p}" for p in platforms],
    ]
    terraform_mod.run(
        argv,
        env=terraform_mod.merged_env(
            state.environ,
            data_dir=config.data_dir_for(state.slot),
            plugin_cache_dir=config.default_plugin_cache_dir,
        ),
    )
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
    state = load_state(config, slot)
    if state is None:
        raise SelfCommandError(
            f"No state for slot {slot!r} at {config.state_file_for(slot)}. Run `tfp use {slot}` first."
        )
    return state


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
        try:
            report = provider.status()
            if report.info.id:
                try:
                    terraform_mod.run(
                        [
                            config.terraform_binary,
                            f"-chdir={state.source_root}",
                            "force-unlock",
                            "-force",
                            report.info.id,
                        ],
                        env=terraform_mod.merged_env(
                            state.environ,
                            data_dir=config.data_dir_for(state.slot),
                            plugin_cache_dir=config.default_plugin_cache_dir,
                        ),
                    )
                except terraform_mod.TerraformExit:
                    pass
                else:
                    return BreakResult(
                        method="force-unlock",
                        backend=provider.backend,
                        lock_id=report.info.id,
                    )
        except lock_mod.LockProviderError:
            pass

    try:
        provider.break_lease()
    except lock_mod.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc
    return BreakResult(method="lease-break", backend=provider.backend)


# ---- helpers ------------------------------------------------------------------


def _resolve_slot_name(config: Config, *, slot: str | None) -> str:
    """Resolve the slot name from `--slot` flag or the active-slot pointer."""
    if slot:
        return slot
    active = slots_mod.resolve_active(config, flag=None)
    if active is None:
        raise SelfCommandError("No active slot. Run `tfp use <slot>` first (or pass --slot <name>).")
    return active.name


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
    "do_self_providers_lock",
    "do_self_snapshot",
    "do_self_state_clear",
    "do_self_state_show",
    "do_self_trace",
]
