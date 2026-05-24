"""Top-level command implementations, decoupled from the CLI layer.

Every wrapped command operates against a `ResolvedSlot` (looked up by name
via the active-slot pointer or `-s` flag) plus the persisted `MyState` from
that slot. `do_use` is the new entry point that replaces the old
`tfp init <tfvars>` flow.
"""

from __future__ import annotations

import hashlib
import pathlib
import platform
import shutil
import sys
from collections.abc import Sequence

from tf_project import (
    preflight,
    slots as slots_mod,
    terraform,
)
from tf_project.config import Config
from tf_project.models import MyState, TfplanMeta, ValueModel
from tf_project.secrets import SecretsProvider, provider_from_config
from tf_project.slots import ActiveSlot, ResolvedSlot
from tf_project.state import (
    load_state,
    save_state,
    try_exclusive_lock,
    write_active,
)

# Linux is always locked (the most common CI/runtime target) regardless of
# where `tfp` is invoked. The host platform is added on top so the lockfile
# also covers wherever the user is running locally (e.g. darwin_arm64 on an
# Apple Silicon Mac). Override with `tfp self providers lock -p <plat>` for
# anything else (windows_amd64, freebsd_amd64, ...).
_BASE_LOCK_PLATFORMS: tuple[str, ...] = ("linux_amd64", "linux_arm64")

_GOOS_BY_SYS_PLATFORM = {"linux": "linux", "darwin": "darwin", "win32": "windows"}
_GOARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "AMD64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def current_terraform_platform() -> str:
    """Return the GOOS_GOARCH string terraform uses for the host."""
    goos = _GOOS_BY_SYS_PLATFORM.get(sys.platform, sys.platform)
    goarch = _GOARCH_BY_MACHINE.get(platform.machine(), platform.machine().lower())
    return f"{goos}_{goarch}"


def default_lock_platforms(extras: Sequence[str] = ()) -> tuple[str, ...]:
    """Linux amd64 + arm64, plus the host platform, plus any configured extras.

    Order is stable (base, host, extras) with duplicates removed so that a
    user listing e.g. `darwin_arm64` in `extra_lock_platforms` from a Linux
    machine still produces a deterministic argv.
    """
    out: list[str] = list(_BASE_LOCK_PLATFORMS)
    for plat in (current_terraform_platform(), *extras):
        if plat not in out:
            out.append(plat)
    return tuple(out)


# Subcommands implemented natively below; everything else falls through to
# `terraform` directly via `do_passthrough`.
#
# Invariant maintained between this module and `cli.py`:
#   - Every entry here MUST have a matching `@app.command(name=...)` in
#     `cli.py` (or live under a `Typer` sub-app whose top-level name is in
#     this set, e.g. "state" / "self").
#   - Anything NOT in this set is forwarded verbatim to `terraform` via
#     `cli._split_passthrough` → `do_passthrough`.
# Adding a new wrapped command requires both an entry here AND the matching
# typer wiring; removing one is the opposite.
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
        # Distinguish "slot doesn't exist" from "slot exists but never inited"
        # so users get a precise error instead of a "no state" message that
        # could mean either.
        try:
            slots_mod.assert_slot_exists(config, slot_name)
        except slots_mod.SlotError as exc:
            raise StateNotInitializedError(str(exc)) from exc
        raise StateNotInitializedError(
            f"No state for slot {slot_name!r} at {config.state_file_for(slot_name)}. Run `tfp use {slot_name}` first."
        )
    return state


def _resolve_active_slot(config: Config, *, flag: str | None) -> tuple[ResolvedSlot, MyState, bool]:
    """Resolve the active slot to its current banner + saved state.

    Returns ``(resolved, state, banner_present)``. The saved state is the
    source of truth for terraform invocations (it captures what was
    init'd, including env that may have drifted from the current banner).
    The banner-derived `ResolvedSlot` is consulted by commands that need
    fresh banner values (e.g. apply-safety checks); `banner_present` tells
    those callers whether `resolved` is in fact derived from a live banner
    or synthesised from saved state.
    """
    active = _require_active(config, flag=flag)
    state = _require_slot_state(config, active.name)
    try:
        resolved = slots_mod.resolve_slot(active.name, config=config)
        return resolved, state, True
    except slots_mod.SlotError:
        # Banner gone - synthesise from saved state. `project` lives on
        # MyState specifically so this fallback stays accurate.
        resolved = ResolvedSlot(
            name=state.slot,
            name_is_explicit=False,
            tfvars=state.tfvars,
            project=state.project,
            source_root=state.source_root,
            env=dict(state.environ),
            backend_config=dict(state.backend_config),
            slot_dir=config.slot_dir_for(state.slot),
        )
        return resolved, state, False


def _provider(config: Config) -> SecretsProvider:
    return provider_from_config(config.secrets)


def _extras(extra: Sequence[str] | None) -> list[str]:
    return list(extra) if extra else []


def _env_for(config: Config, state: MyState) -> dict[str, str]:
    return terraform.env_for_slot(config, state)


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
    banner_present: bool,
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
    # The banner-drift comparison only makes sense against a live banner.
    # If the tfvars has been moved/deleted, `resolved` is synthesised from
    # the same saved state that `meta` was written against - the comparison
    # would be tautologically equal and the drift check silently no-ops. We
    # warn the user explicitly rather than pretend the check ran.
    if not banner_present:
        print(
            f"warning: tfvars banner for slot {state.slot!r} is missing; "
            "skipping banner-drift check on apply. Re-run `tfp use` once "
            "the banner is back to revalidate.",
            file=sys.stderr,
        )
        return
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


def build_providers_lock_argv(
    config: Config,
    *,
    source_root: pathlib.Path,
    platforms: Sequence[str] | None = None,
) -> list[str]:
    raw = platforms if platforms is not None else default_lock_platforms(config.extra_lock_platforms)
    # Dedupe while preserving order: a user passing `-p linux_amd64 -p linux_amd64`
    # or whose extras overlap the defaults shouldn't generate duplicate flags.
    plats = list(dict.fromkeys(raw))
    return [
        config.terraform_binary,
        f"-chdir={source_root}",
        "providers",
        "lock",
        *[f"-platform={p}" for p in plats],
    ]


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


def build_output_argv(config: Config, *, state: MyState, extra: Sequence[str] | None = None) -> list[str]:
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


def do_use(config: Config, *, arg: str, extra: Sequence[str] | None = None) -> ResolvedSlot:
    """Resolve `arg` to a slot, init if needed, and set it as the active slot.

    `arg` is a slot name or a path to a tfvars file. Validates uniqueness
    across the tfvars directory. Runs `terraform init -reconfigure` only
    when the slot's `.terraform/` is missing or the backend signature has
    changed; otherwise the switch is a pointer write.
    """
    validate_uniqueness(config)
    resolved = slots_mod.resolve_slot(arg, config=config)

    if arg != resolved.name and slots_mod.looks_like_path(arg):
        print(f"resolved {arg} → slot {resolved.name!r}", file=sys.stderr)

    decision = _decide_init(config, resolved, config.terraform_binary)
    first_init = not resolved.slot_dir.exists()
    resolved.slot_dir.mkdir(parents=True, exist_ok=True)
    plugin_cache_dir = config.effective_plugin_cache_dir
    if plugin_cache_dir is not None:
        plugin_cache_dir.mkdir(parents=True, exist_ok=True)

    if decision.needs_init:
        print(f"==== PROJECT: {resolved.project} (slot {resolved.name!r}, {decision.reason}) ====")
        init_env = terraform.merged_env(
            resolved.env,
            data_dir=resolved.data_dir,
            plugin_cache_dir=plugin_cache_dir,
        )
        try:
            terraform.run(
                build_init_argv(config, resolved=resolved, extra=extra),
                env=init_env,
            )
            terraform.run(
                build_providers_lock_argv(config, source_root=resolved.source_root),
                env=init_env,
            )
        except terraform.TerraformExit:
            # On first-init failure, wipe the partially-created slot dir so
            # a retry starts clean. On re-init failure (slot already had a
            # previously-good state), keep the existing state.json /
            # backend.hash intact - the user can still operate on the
            # last-known-good init while diagnosing.
            if first_init:
                shutil.rmtree(resolved.slot_dir, ignore_errors=True)
            else:
                print(
                    f"init failed; slot {resolved.name!r} retains its previous "
                    "saved state - fix the cause and re-run `tfp use`.",
                    file=sys.stderr,
                )
            raise
        config.backend_hash_for(resolved.name).write_text(_backend_signature(resolved, config.terraform_binary) + "\n")

    state = MyState(
        slot=resolved.name,
        project=resolved.project,
        tfvars=resolved.tfvars,
        source_root=resolved.source_root,
        tfplan_location=config.tfplan_file_for(resolved.name),
        environ=dict(resolved.env),
        backend_config=dict(resolved.backend_config),
    )
    save_state(state, config)
    write_active(config, resolved.name)

    print(
        f"active slot → {resolved.name} (file: {config.active_file}; affects all shells without TFP_SLOT set)",
        file=sys.stderr,
    )
    return resolved


def validate_uniqueness(config: Config) -> None:
    """Ensure all banners produce distinct slot names; raise on conflict."""
    slots_mod.validate_unique(slots_mod.list_slots(config))


def do_rm(config: Config, *, name: str, force: bool, slot: str | None = None) -> pathlib.Path:
    """Remove the on-disk slot directory.

    `name` is the slot to remove. `slot` is the (optional) active-slot
    override used to evaluate the "refuse on active" guard - matches the
    convention used by every other ``do_*`` in this module.
    """
    active = slots_mod.resolve_active(config, flag=slot)
    if active is not None and active.name == name and not force:
        raise slots_mod.SlotInUseError(
            f"slot {name!r} is the active slot ({active.source}). Pass --force to remove it anyway."
        )
    slot_dir = config.slot_dir_for(name)
    if not slot_dir.exists():
        raise slots_mod.SlotNotFoundError(f"no slot directory at {slot_dir}")
    shutil.rmtree(slot_dir)
    return slot_dir


def do_plan(
    config: Config,
    *,
    slot: str | None = None,
    targets: Sequence[str] | None = None,
    replaces: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    resolved, state, banner_present = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    with _provider(config).materialize(state.tfvars) as decrypted:
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
    slot: str | None = None,
    force: bool = False,
    extra: Sequence[str] | None = None,
) -> None:
    resolved, state, banner_present = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    preflight.check_apply_plan(config, state.slot)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with _provider(config).materialize(state.tfvars) as decrypted:
            if not force:
                _check_tfplan_fresh(
                    config,
                    state=state,
                    resolved=resolved,
                    decrypted=decrypted,
                    banner_present=banner_present,
                )
            terraform.run(
                build_apply_argv(config, state=state, var_file=str(decrypted), extra=extra),
                env=_env_for(config, state),
            )
        config.tfplan_file_for(state.slot).unlink(missing_ok=True)
        config.tfplan_meta_for(state.slot).unlink(missing_ok=True)


def do_refresh(
    config: Config,
    *,
    slot: str | None = None,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state, _ = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with _provider(config).materialize(state.tfvars) as decrypted:
            terraform.run(
                build_refresh_argv(config, state=state, var_file=str(decrypted), targets=targets, extra=extra),
                env=_env_for(config, state),
            )


def do_destroy(
    config: Config,
    *,
    slot: str | None = None,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state, _ = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    with try_exclusive_lock(_apply_lock_path(config, state.slot)):
        with _provider(config).materialize(state.tfvars) as decrypted:
            terraform.run(
                build_destroy_argv(config, state=state, var_file=str(decrypted), targets=targets, extra=extra),
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
    slot: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    _, state, _ = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    terraform.run(build_output_argv(config, state=state, extra=extra), env=_env_for(config, state))


def do_import(
    config: Config,
    *,
    address: str,
    resource_id: str,
    slot: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    from tf_project.self_commands import auto_snapshot

    _, state, _ = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    auto_snapshot(config, state, op="import")
    with _provider(config).materialize(state.tfvars) as decrypted:
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


STATE_MUTATING_SUBCOMMANDS: frozenset[str] = frozenset({"rm", "mv", "replace-provider"})


def do_state(
    config: Config,
    *,
    subcommand: str,
    args: Sequence[str],
    slot: str | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    from tf_project.self_commands import auto_snapshot

    _, state, _ = _resolve_active_slot(config, flag=slot)
    preflight.check_data_dir(config, state.slot)
    if subcommand in STATE_MUTATING_SUBCOMMANDS:
        auto_snapshot(config, state, op=f"state-{subcommand}")
    terraform.run(
        build_state_argv(config, state=state, subcommand=subcommand, args=args, extra=extra),
        env=_env_for(config, state),
    )


def do_passthrough(
    config: Config,
    args: Sequence[str],
    *,
    slot: str | None = None,
) -> None:
    """Forward an arbitrary terraform invocation by replacing this process.

    Uses `os.execvpe` so signals and exit code are fully native - no Python
    in the loop. Returns only in dry-run mode (or in tests, where
    `exec_passthrough` is mocked).

    Slot resolution:
      - No active slot at all → run terraform with neither `-chdir` nor
        slot env (e.g. `tfp version`, `tfp self init`-like cases).
      - Active slot resolves but has no saved state (e.g. `-s prod` for a
        never-`tfp use`'d slot) → refuse with StateNotInitializedError.
        Silently dropping `-chdir` here would misroute commands.
      - Active slot + saved state → normal path with `-chdir` and slot env.
    """
    active = slots_mod.resolve_active(config, flag=slot)
    cmd: list[str] = [config.terraform_binary]
    env: dict[str, str] | None = None
    if active is not None:
        # Use the same helper as the wrapped commands so the error message
        # distinguishes "no such slot" from "slot exists, never inited".
        state = _require_slot_state(config, active.name)
        preflight.check_data_dir(config, state.slot)
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


def status_report(config: Config, *, slot: str | None = None) -> StatusReport:
    active = slots_mod.resolve_active(config, flag=slot)
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


def slot_listing(config: Config, *, slot: str | None = None) -> list[SlotListing]:
    import datetime

    active = slots_mod.resolve_active(config, flag=slot)
    active_name = active.name if active is not None else None
    out: list[SlotListing] = []
    for resolved in slots_mod.list_slots(config):
        # A slot is "warm" once `.terraform/providers/` is populated by
        # `terraform init`. That's independent of remote-vs-local backend
        # (remote backends also write `.terraform/terraform.tfstate` as a
        # backend-binding marker, but local backends do not). Show the
        # providers-dir mtime so users can see how stale the init is.
        marker = resolved.data_dir / "providers"
        warm = marker.is_dir()
        mtime: str | None = None
        if warm:
            ts = datetime.datetime.fromtimestamp(marker.stat().st_mtime, datetime.timezone.utc)
            mtime = ts.isoformat(timespec="seconds")
        out.append(
            SlotListing(
                name=resolved.name,
                project=resolved.project,
                tfvars=resolved.tfvars,
                backend_key=resolved.backend_config.get("key"),
                warm=warm,
                warm_mtime=mtime,
                is_active=(resolved.name == active_name),
            )
        )
    return out


__all__ = [
    "InitDecision",
    "NoActiveSlotError",
    "SlotListing",
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
    "do_rm",
    "do_use",
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
