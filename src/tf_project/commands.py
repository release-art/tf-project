"""Top-level command implementations, decoupled from the CLI layer.

Every wrapped command operates against a `ResolvedSlot` (looked up by name
via the active-slot pointer or `-s` flag) plus the persisted `MyState` from
that slot. `cmd_use` is the new entry point that replaces the old
`tfp init <tfvars>` flow.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import sys
from collections.abc import Sequence

from tf_project import (
    slots as slots_mod,
    terraform,
)
from tf_project.config import Config
from tf_project.models import MyState, TfplanMeta, ValueModel
from tf_project.secrets import SecretsProvider, provider_from_config
from tf_project.slots import ActiveSlot, ResolvedSlot
from tf_project.state import (
    decrypted_tfvars,
    load_state,
    save_state,
    try_exclusive_lock,
    write_active,
)

# Subcommands implemented natively below; everything else falls through to
# `terraform` directly via `do_passthrough`.
WRAPPED_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "use",
        "ls",
        "rm",
        "plan",
        "apply",
        "refresh",
        "destroy",
        "fmt",
        "output",
        "state",
        "status",
        "last",
        "import",
    }
)


class StateNotInitializedError(RuntimeError):
    """Raised when a command needs a saved state but `tfp use` hasn't been run."""


class StaleTfplanError(RuntimeError):
    """The saved tfplan was generated against different inputs."""


class NoActiveSlotError(RuntimeError):
    """No active slot resolvable from -s / TFP_SLOT / tmp/active."""


def _apply_lock_path(config: Config, slot: str) -> pathlib.Path:
    return config.slot_dir_for(slot) / "apply.lock"


def _require_active(config: Config, *, flag: str | None) -> ActiveSlot:
    active = slots_mod.resolve_active(config, flag=flag)
    if active is None:
        raise NoActiveSlotError("No active slot. Run `tfp use <slot>` first (or pass `-s <slot>` / set TFP_SLOT).")
    return active


def _require_slot_state(config: Config, slot_name: str) -> MyState:
    state = load_state(config, slot_name)
    if state is None:
        raise StateNotInitializedError(
            f"No state for slot {slot_name!r} at {config.state_file_for(slot_name)}. "
            f"Run `tfp use {slot_name}` first."
        )
    return state


def _resolve_active_slot(config: Config, *, flag: str | None) -> tuple[ResolvedSlot, MyState]:
    """Resolve the active slot to its current banner + saved state.

    The saved state is the source of truth for terraform invocations (it
    captures what was init'd, including env that may have drifted from the
    current banner). The banner-derived `ResolvedSlot` is consulted by
    commands that need fresh banner values (e.g. apply-safety checks).
    """
    active = _require_active(config, flag=flag)
    state = _require_slot_state(config, active.name)
    # Best-effort: look up the banner for the slot too. If the tfvars has
    # been moved/deleted since `tfp use`, fall back to a synthetic
    # ResolvedSlot built from saved state.
    try:
        resolved = slots_mod.resolve_slot(active.name, config=config)
    except slots_mod.SlotError:
        resolved = ResolvedSlot(
            name=state.slot,
            name_is_explicit=False,
            tfvars=state.tfvars,
            project=state.tfvars.stem,  # best effort
            source_root=state.source_root,
            env=dict(state.environ),
            backend_config=dict(state.backend_config),
            slot_dir=config.slot_dir_for(state.slot),
        )
    return resolved, state


def _provider(config: Config) -> SecretsProvider:
    return provider_from_config(config.secrets)


def _extras(extra: Sequence[str] | None) -> list[str]:
    return list(extra) if extra else []


def _env_for(config: Config, state: MyState) -> dict[str, str]:
    return terraform.merged_env(
        state.environ,
        data_dir=config.data_dir_for(state.slot),
        plugin_cache_dir=config.default_plugin_cache_dir,
    )


# ---- Apply-safety -------------------------------------------------------------


def _plan_inputs_sha256(state: MyState, decrypted: pathlib.Path) -> str:
    """Rolling hash over the decrypted tfvars plus every `.tf` under source_root."""
    h = hashlib.sha256()
    h.update(b"tfvars:")
    h.update(decrypted.read_bytes())
    source_root = state.source_root
    if source_root.is_dir():
        for tf_file in sorted(source_root.rglob("*.tf")):
            rel = tf_file.relative_to(source_root)
            h.update(b"\x00tf:")
            h.update(str(rel).encode("utf-8"))
            h.update(b"\x00")
            h.update(tf_file.read_bytes())
    return h.hexdigest()


def _write_tfplan_meta(
    config: Config,
    *,
    state: MyState,
    resolved: ResolvedSlot,
    decrypted: pathlib.Path,
) -> None:
    meta = TfplanMeta(
        inputs_sha256=_plan_inputs_sha256(state, decrypted),
        project=resolved.project,
        state_key=resolved.backend_config.get("key"),
        backend_config=dict(resolved.backend_config),
        env=dict(resolved.env),
    )
    meta.dump_file(config.tfplan_meta_for(state.slot))


def _check_tfplan_fresh(
    config: Config,
    *,
    state: MyState,
    resolved: ResolvedSlot,
    decrypted: pathlib.Path,
) -> None:
    meta_path = config.tfplan_meta_for(state.slot)
    if not meta_path.exists():
        return
    meta = TfplanMeta.load_file(meta_path)
    actual = _plan_inputs_sha256(state, decrypted)
    if meta.inputs_sha256 != actual:
        raise StaleTfplanError(
            f"Saved tfplan at {config.tfplan_file_for(state.slot)} was generated against different "
            f"inputs (sha256 {meta.inputs_sha256[:12]}…) than the current tfvars + .tf source "
            f"(sha256 {actual[:12]}…). Re-run `tfp plan` or use `tfp apply --force`."
        )
    drift = []
    if meta.project != resolved.project:
        drift.append(f"project {meta.project!r} → {resolved.project!r}")
    if meta.state_key != resolved.backend_config.get("key"):
        drift.append(f"state_key {meta.state_key!r} → {resolved.backend_config.get('key')!r}")
    if meta.backend_config != resolved.backend_config:
        drift.append("backend_config changed")
    if meta.env != resolved.env:
        drift.append("banner env changed")
    if drift:
        raise StaleTfplanError(
            f"Saved tfplan for slot {state.slot!r} no longer matches the current banner: "
            f"{'; '.join(drift)}. Re-run `tfp plan` or use `tfp apply --force`."
        )


# ---- Argv builders ------------------------------------------------------------


def build_init_argv(
    config: Config,
    *,
    resolved: ResolvedSlot,
    extra: Sequence[str] | None = None,
) -> list[str]:
    cmd = [
        config.terraform_binary,
        f"-chdir={resolved.source_root}",
        "init",
        "-upgrade",
        "-reconfigure",
        *_extras(extra),
    ]
    for key, value in resolved.backend_config.items():
        cmd.extend(["-backend-config", f"{key}={value}"])
    return cmd


def build_plan_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    replaces: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "plan",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *terraform.replace_args(replaces),
        *_extras(extra),
        f"-out={config.tfplan_file_for(state.slot)}",
    ]


def build_apply_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "apply",
        f"-var-file={var_file}",
        *_extras(extra),
        str(config.tfplan_file_for(state.slot)),
    ]


def build_refresh_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "apply",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *_extras(extra),
    ]


def build_destroy_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "destroy",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *_extras(extra),
    ]


def build_state_argv(
    config: Config,
    *,
    state: MyState,
    subcommand: str,
    args: Sequence[str],
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "state",
        subcommand,
        *_extras(extra),
        *args,
    ]


def build_import_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    address: str,
    resource_id: str,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "import",
        f"-var-file={var_file}",
        *_extras(extra),
        address,
        resource_id,
    ]


def build_output_argv(
    config: Config, *, state: MyState, extra: Sequence[str] | None = None
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "output",
        "-json",
        *_extras(extra),
    ]


# ---- backend.hash skip-check --------------------------------------------------


def _backend_signature(resolved: ResolvedSlot, terraform_binary: str) -> str:
    h = hashlib.sha256()
    h.update(b"tf:")
    h.update(terraform_binary.encode("utf-8"))
    h.update(b"\x00src:")
    h.update(str(resolved.source_root).encode("utf-8"))
    for key in sorted(resolved.backend_config):
        h.update(b"\x00bc:")
        h.update(key.encode("utf-8"))
        h.update(b"=")
        h.update(resolved.backend_config[key].encode("utf-8"))
    return h.hexdigest()


class InitDecision(ValueModel):
    needs_init: bool
    reason: str


def _decide_init(config: Config, resolved: ResolvedSlot, terraform_binary: str) -> InitDecision:
    if not resolved.data_dir.is_dir():
        return InitDecision(needs_init=True, reason="first init (no .terraform/)")
    hash_path = config.backend_hash_for(resolved.name)
    if not hash_path.exists():
        return InitDecision(needs_init=True, reason="missing backend.hash")
    expected = _backend_signature(resolved, terraform_binary)
    if hash_path.read_text().strip() != expected:
        return InitDecision(needs_init=True, reason="backend_config changed")
    return InitDecision(needs_init=False, reason="warm")


# ---- Commands ----------------------------------------------------------------


def cmd_use(config: Config, *, arg: str, extra: Sequence[str] | None = None) -> ResolvedSlot:
    """Resolve `arg` to a slot, init if needed, and set it as the active slot.

    `arg` is a slot name or a path to a tfvars file. Validates uniqueness
    across the tfvars directory. Runs `terraform init -reconfigure` only
    when the slot's `.terraform/` is missing or the backend signature has
    changed; otherwise the switch is a pointer write.
    """
    validate_uniqueness(config)
    resolved = slots_mod.resolve_slot(arg, config=config)

    if arg != resolved.name and not _is_pure_name(arg):
        print(f"resolved {arg} → slot {resolved.name!r}", file=sys.stderr)

    decision = _decide_init(config, resolved, config.terraform_binary)
    resolved.slot_dir.mkdir(parents=True, exist_ok=True)
    if config.plugin_cache_enabled:
        config.default_plugin_cache_dir.mkdir(parents=True, exist_ok=True)

    if decision.needs_init:
        print(f"==== PROJECT: {resolved.project} (slot {resolved.name!r}, {decision.reason}) ====")
        try:
            terraform.run(
                build_init_argv(config, resolved=resolved, extra=extra),
                env=terraform.merged_env(
                    resolved.env,
                    data_dir=resolved.data_dir,
                    plugin_cache_dir=config.default_plugin_cache_dir,
                ),
            )
        except terraform.TerraformExit:
            shutil.rmtree(resolved.slot_dir, ignore_errors=True)
            raise
        config.backend_hash_for(resolved.name).write_text(
            _backend_signature(resolved, config.terraform_binary) + "\n"
        )

    state = MyState(
        slot=resolved.name,
        tfvars=resolved.tfvars,
        source_root=resolved.source_root,
        tfplan_location=config.tfplan_file_for(resolved.name),
        environ=dict(resolved.env),
        backend_config=dict(resolved.backend_config),
    )
    save_state(state, config)
    write_active(config, resolved.name)

    print(
        f"active slot → {resolved.name} "
        f"(file: {config.active_file}; affects all shells without TFP_SLOT set)",
        file=sys.stderr,
    )
    return resolved


def validate_uniqueness(config: Config) -> None:
    """Ensure all banners produce distinct slot names; raise on conflict."""
    slots_mod.validate_unique(slots_mod.list_slots(config))


def cmd_rm(config: Config, *, slot: str, force: bool, flag_active: str | None = None) -> pathlib.Path:
    """Remove the on-disk slot directory. Refuses on the active slot unless `force`."""
    active = slots_mod.resolve_active(config, flag=flag_active)
    if active is not None and active.name == slot and not force:
        raise SlotInUseError(
            f"slot {slot!r} is the active slot ({active.source}). "
            "Pass --force to remove it anyway."
        )
    slot_dir = config.slot_dir_for(slot)
    if not slot_dir.exists():
        raise SlotNotFoundError(f"no slot directory at {slot_dir}")
    shutil.rmtree(slot_dir)
    return slot_dir


class SlotInUseError(RuntimeError):
    """`tfp rm <slot>` blocked because the slot is active."""


class SlotNotFoundError(RuntimeError):
    """`tfp rm <slot>` could not find the slot directory."""


def do_plan(
    config: Config,
    *,
    flag_active: str | None = None,
    targets: Sequence[str] | None = None,
    replaces: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    resolved, state = _resolve_active_slot(config, flag=flag_active)
    with decrypted_tfvars(state, _provider(config)) as decrypted:
        terraform.run(
            build_plan_argv(
                config,
                state=state,
                var_file=str(decrypted),
                targets=targets,
                replaces=replaces,
                extra=extra,
            ),
            env=_env_for(config, state),
        )
        _write_tfplan_meta(config, state=state, resolved=resolved, decrypted=decrypted)


def do_apply(
    config: Config,
    *,
    flag_active: str | None = None,
    force: bool = False,
    extra: Sequence[str] | None = None,
) -> None:
    resolved, state = _resolve_active_slot(config, flag=flag_active)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with decrypted_tfvars(state, _provider(config)) as decrypted:
            if not force:
                _check_tfplan_fresh(config, state=state, resolved=resolved, decrypted=decrypted)
            terraform.run(
                build_apply_argv(config, state=state, var_file=str(decrypted), extra=extra),
                env=_env_for(config, state),
            )
        config.tfplan_file_for(state.slot).unlink(missing_ok=True)
        config.tfplan_meta_for(state.slot).unlink(missing_ok=True)


def do_refresh(
    config: Config,
    *,
    flag_active: str | None = None,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state = _resolve_active_slot(config, flag=flag_active)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with decrypted_tfvars(state, _provider(config)) as decrypted:
            terraform.run(
                build_refresh_argv(
                    config, state=state, var_file=str(decrypted), targets=targets, extra=extra
                ),
                env=_env_for(config, state),
            )


def do_destroy(
    config: Config,
    *,
    flag_active: str | None = None,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state = _resolve_active_slot(config, flag=flag_active)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with decrypted_tfvars(state, _provider(config)) as decrypted:
            terraform.run(
                build_destroy_argv(
                    config, state=state, var_file=str(decrypted), targets=targets, extra=extra
                ),
                env=_env_for(config, state),
            )


def do_fmt(config: Config, *, extra: Sequence[str] | None = None) -> None:
    paths: list[str] = []
    if config.terraform_dir.exists():
        paths.append(str(config.terraform_dir))
    if config.tfvars_dir.exists():
        paths.append(str(config.tfvars_dir))
    if not paths:
        raise FileNotFoundError(f"Neither {config.terraform_dir} nor {config.tfvars_dir} exists to format.")
    terraform.run([config.terraform_binary, "fmt", "-recursive", *_extras(extra), *paths])


def do_output(
    config: Config,
    *,
    flag_active: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state = _resolve_active_slot(config, flag=flag_active)
    terraform.run(build_output_argv(config, state=state, extra=extra), env=_env_for(config, state))


def do_import(
    config: Config,
    *,
    address: str,
    resource_id: str,
    flag_active: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state = _resolve_active_slot(config, flag=flag_active)
    with decrypted_tfvars(state, _provider(config)) as decrypted:
        terraform.run(
            build_import_argv(
                config,
                state=state,
                var_file=str(decrypted),
                address=address,
                resource_id=resource_id,
                extra=extra,
            ),
            env=_env_for(config, state),
        )


def do_state(
    config: Config,
    *,
    subcommand: str,
    args: Sequence[str],
    flag_active: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state = _resolve_active_slot(config, flag=flag_active)
    terraform.run(
        build_state_argv(config, state=state, subcommand=subcommand, args=args, extra=extra),
        env=_env_for(config, state),
    )


def do_passthrough(
    config: Config,
    args: Sequence[str],
    *,
    flag_active: str | None = None,
) -> None:
    """Forward an arbitrary terraform invocation by replacing this process.

    Uses `os.execvpe` so signals and exit code are fully native — no Python
    in the loop. Returns only in dry-run mode (or in tests, where
    `exec_passthrough` is mocked).
    """
    active = slots_mod.resolve_active(config, flag=flag_active)
    state = load_state(config, active.name) if active is not None else None
    cmd: list[str] = [config.terraform_binary]
    env: dict[str, str] | None = None
    if state is not None:
        cmd.append(f"-chdir={state.source_root}")
        env = _env_for(config, state)
    cmd.extend(args)
    terraform.exec_passthrough(cmd, env=env)


# ---- Status / ls --------------------------------------------------------------


class StatusReport(ValueModel):
    initialized: bool
    slot: str | None = None
    slot_source: str | None = None
    tfvars: str | None = None
    source_root: str | None = None
    backend_key: str | None = None
    env_keys: tuple[str, ...] = ()
    plan_ready: bool = False


def status_report(config: Config, *, flag_active: str | None = None) -> StatusReport:
    active = slots_mod.resolve_active(config, flag=flag_active)
    if active is None:
        return StatusReport(initialized=False)
    state = load_state(config, active.name)
    if state is None:
        return StatusReport(initialized=False, slot=active.name, slot_source=active.source)
    return StatusReport(
        initialized=True,
        slot=state.slot,
        slot_source=active.source,
        tfvars=str(state.tfvars),
        source_root=str(state.source_root),
        backend_key=state.backend_config.get("key"),
        env_keys=tuple(sorted(state.environ)),
        plan_ready=config.tfplan_file_for(state.slot).exists(),
    )


class SlotListing(ValueModel):
    name: str
    project: str
    tfvars: pathlib.Path
    backend_key: str | None
    warm: bool
    warm_mtime: str | None
    is_active: bool


def slot_listing(config: Config, *, flag_active: str | None = None) -> list[SlotListing]:
    import datetime

    active = slots_mod.resolve_active(config, flag=flag_active)
    active_name = active.name if active is not None else None
    out: list[SlotListing] = []
    for slot in slots_mod.list_slots(config):
        # A slot is "warm" once `.terraform/providers/` is populated by
        # `terraform init`. That's independent of remote-vs-local backend
        # (remote backends also write `.terraform/terraform.tfstate` as a
        # backend-binding marker, but local backends do not). Show the
        # providers-dir mtime so users can see how stale the init is.
        marker = slot.data_dir / "providers"
        warm = marker.is_dir()
        mtime: str | None = None
        if warm:
            ts = datetime.datetime.fromtimestamp(marker.stat().st_mtime, datetime.timezone.utc)
            mtime = ts.isoformat(timespec="seconds")
        out.append(
            SlotListing(
                name=slot.name,
                project=slot.project,
                tfvars=slot.tfvars,
                backend_key=slot.backend_config.get("key"),
                warm=warm,
                warm_mtime=mtime,
                is_active=(slot.name == active_name),
            )
        )
    return out


def _is_pure_name(arg: str) -> bool:
    return not (arg.endswith(".tfvars") or "/" in arg or "\\" in arg)


__all__ = [
    "InitDecision",
    "NoActiveSlotError",
    "SlotInUseError",
    "SlotListing",
    "SlotNotFoundError",
    "StaleTfplanError",
    "StateNotInitializedError",
    "StatusReport",
    "WRAPPED_SUBCOMMANDS",
    "build_apply_argv",
    "build_destroy_argv",
    "build_import_argv",
    "build_init_argv",
    "build_output_argv",
    "build_plan_argv",
    "build_refresh_argv",
    "build_state_argv",
    "cmd_rm",
    "cmd_use",
    "do_apply",
    "do_destroy",
    "do_fmt",
    "do_import",
    "do_output",
    "do_passthrough",
    "do_plan",
    "do_refresh",
    "do_state",
    "slot_listing",
    "status_report",
    "validate_uniqueness",
]
