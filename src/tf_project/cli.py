"""Typer CLI for tf_project."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Annotated, NamedTuple

import typer

from tf_project import banner, commands, migrate, preflight, self_commands, slots, terraform
from tf_project.__version__ import __version__
from tf_project.commands import WRAPPED_SUBCOMMANDS
from tf_project.config import Config, ConfigError
from tf_project.state import LockBusyError

SELF_SUBCOMMAND = "self"
GLOBAL_FLAGS = {"--verbose", "--dry-run"}

PASSTHROUGH_CTX = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}


class _CliState:
    """Holds parsed global flags between root callback and subcommands."""

    def __init__(self) -> None:
        self.config: Config | None = None
        self.slot: str | None = None


def _cli_state(ctx: typer.Context) -> _CliState:
    if not isinstance(ctx.obj, _CliState):
        ctx.obj = _CliState()
    return ctx.obj


def _config(ctx: typer.Context) -> Config:
    state = _cli_state(ctx)
    if state.config is None:  # pragma: no branch - cached path only reachable across multiple in-process calls
        state.config = Config.discover()
        migrate.run_if_needed(state.config)
    return state.config


def _slot_flag(ctx: typer.Context) -> str | None:
    return _cli_state(ctx).slot


app = typer.Typer(
    name="tf-project",
    help=(
        "Custom Terraform project wrapper. Unknown subcommands are forwarded "
        "to `terraform` verbatim. Global flags: --verbose (echo terraform "
        "argv to stderr), --dry-run (print argv and skip execution), "
        "-s/--slot (operate against a specific slot for this invocation)."
    ),
    no_args_is_help=True,
)


# Shell-completion callbacks, wired via typer's native `autocompletion=` (not
# click's `shell_complete=`, which typer deprecates). typer invokes each as
# `cb(ctx, args, incomplete)` and turns a returned `(value, help)` tuple into a
# completion with help text (typer/core.py). `_Completion` is that tuple as a
# NamedTuple, so the items also expose `.value` / `.help` for tests - and we
# avoid importing the standalone `click` package, which is not a dependency
# (typer vendors its own click as `typer._click`).
class _Completion(NamedTuple):
    value: str
    help: str


def _complete_tfvars(ctx: typer.Context, args: list[str], incomplete: str) -> list[_Completion]:
    """Tab-completion: list tfvars files under config.tfvars_dir, labelled by project."""
    try:
        cfg = Config.discover()
    except Exception:  # noqa: BLE001
        return []
    out: list[_Completion] = []
    if not cfg.tfvars_dir.is_dir():
        return out
    cwd = pathlib.Path.cwd()
    for path in sorted(cfg.tfvars_dir.rglob("*.tfvars")):
        try:
            display = str(path.relative_to(cwd))
        except ValueError:
            display = str(path)
        if not display.startswith(incomplete):
            continue
        try:
            b = banner.find_banner(path)
            label = b.project
        except (banner.ProjectInfoNotFoundError, banner.BannerError, OSError):
            label = "(no banner)"
        out.append(_Completion(display, f"project={label}"))
    return out


def _complete_slot(ctx: typer.Context, args: list[str], incomplete: str) -> list[_Completion]:
    """Tab-completion: slot names from the tfvars dir."""
    try:
        cfg = Config.discover()
    except Exception:  # noqa: BLE001
        return []
    out: list[_Completion] = []
    for slot in slots.list_slots(cfg):
        if slot.name.startswith(incomplete):
            out.append(_Completion(slot.name, f"project={slot.project}"))
    return out


def _complete_use_arg(ctx: typer.Context, args: list[str], incomplete: str) -> list[_Completion]:
    """Tab-completion for `tfp use`: slot names + tfvars paths."""
    return _complete_slot(ctx, args, incomplete) + _complete_tfvars(ctx, args, incomplete)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    slot_flag: Annotated[
        str | None,
        typer.Option(
            "--slot",
            "-s",
            help="Operate against a specific slot for this invocation (does not change the saved active).",
            autocompletion=_complete_slot,
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Custom Terraform project wrapper."""
    _cli_state(ctx).slot = slot_flag


TargetsOption = Annotated[
    list[str] | None,
    typer.Option("--target", "-t", metavar="RESOURCE", help="Limit to a resource address (repeatable)."),
]
ReplacesOption = Annotated[
    list[str] | None,
    typer.Option("--replace", "-r", metavar="RESOURCE", help="Force replacement of a resource (repeatable)."),
]


# ---- use / ls / rm ------------------------------------------------------------


@app.command(
    "use",
    context_settings=PASSTHROUGH_CTX,
    help="Switch to a slot. Argument is a slot name or a tfvars path.",
)
def use(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Argument(help="Slot name (banner `slot` field, or tfvars stem) or path to a tfvars file."),
    ],
) -> None:
    cfg = _config(ctx)
    try:
        commands.do_use(cfg, arg=target, extra=ctx.args)
    except slots.SlotConflictError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except slots.SlotError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command(
    "init",
    context_settings=PASSTHROUGH_CTX,
    help=(
        "Re-resolve the active slot's banner and re-bind the backend "
        "(terraform init -reconfigure with the banner's -backend-config), "
        "refreshing the saved slot state. Run this after editing a banner's "
        "state_key / backend_config. Does not change which slot is active."
    ),
)
def init(ctx: typer.Context) -> None:
    commands.do_init(_config(ctx), slot=_slot_flag(ctx), extra=ctx.args)


@app.command("ls", help="List all slots derived from tfvars banners.")
def ls(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    cfg = _config(ctx)
    listing = commands.slot_listing(cfg, slot=_slot_flag(ctx))
    orphans = slots.list_orphans(cfg)
    # `ls` is the diagnostic surface - surface duplicate banner slot names
    # here rather than silently dropping the loser of `list_slots` ordering.
    try:
        slots.validate_unique(slots.list_slots(cfg))
    except slots.SlotConflictError as exc:
        typer.echo(f"warning: {exc}", err=True)
    if as_json:
        payload = {
            "slots": [s.model_dump(mode="json") for s in listing],
            "orphans": [o.model_dump(mode="json") for o in orphans],
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not listing:
        typer.echo("(no slots found in tfvars_dir)")
    else:
        rows = [("", "SLOT", "PROJECT", "TFVARS", "BACKEND KEY", "WARM SINCE")]
        for s in listing:
            rows.append(
                (
                    "*" if s.is_active else " ",
                    s.name,
                    s.project,
                    str(s.tfvars),
                    s.backend_key or "-",
                    s.warm_mtime or "-",
                )
            )
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for r in rows:
            typer.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(r)))
    if orphans:
        typer.echo("")
        typer.echo("orphans (slot dirs with no matching banner):")
        for o in orphans:
            typer.echo(f"  {o.name}    {o.slot_dir}    (run `tfp rm {o.name}` to remove)")


@app.command("rm", help="Remove a slot directory (does not touch the tfvars file).")
def rm(
    ctx: typer.Context,
    slot: Annotated[str, typer.Argument(help="Slot name to remove.", autocompletion=_complete_slot)],
    force: Annotated[bool, typer.Option("--force", help="Allow removing the active slot.")] = False,
) -> None:
    cfg = _config(ctx)
    try:
        removed = commands.do_rm(cfg, name=slot, force=force, slot=_slot_flag(ctx))
    except (slots.SlotInUseError, slots.SlotNotFoundError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"removed {removed}")


# ---- Wrapped terraform subcommands -------------------------------------------


@app.command("plan", context_settings=PASSTHROUGH_CTX, help="Run `terraform plan` against the active slot.")
def plan(
    ctx: typer.Context,
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    commands.do_plan(
        _config(ctx),
        slot=_slot_flag(ctx),
        targets=targets,
        replaces=replaces,
        extra=ctx.args,
    )


@app.command("apply", context_settings=PASSTHROUGH_CTX, help="Apply the saved tfplan.")
def apply(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="Apply even if plan inputs changed.")] = False,
) -> None:
    commands.do_apply(_config(ctx), slot=_slot_flag(ctx), force=force, extra=ctx.args)


@app.command(
    "refresh",
    context_settings=PASSTHROUGH_CTX,
    help="Apply directly (without a saved plan), optionally targeted.",
)
def refresh(ctx: typer.Context, targets: TargetsOption = None) -> None:
    commands.do_refresh(_config(ctx), slot=_slot_flag(ctx), targets=targets, extra=ctx.args)


@app.command("destroy", context_settings=PASSTHROUGH_CTX, help="Run `terraform destroy`, optionally targeted.")
def destroy(ctx: typer.Context, targets: TargetsOption = None) -> None:
    commands.do_destroy(_config(ctx), slot=_slot_flag(ctx), targets=targets, extra=ctx.args)


@app.command(
    "fmt",
    context_settings=PASSTHROUGH_CTX,
    help="Recursively `terraform fmt` the terraform/ and tfvars/ trees.",
)
def fmt(ctx: typer.Context) -> None:
    commands.do_fmt(_config(ctx), extra=ctx.args)


@app.command("output", context_settings=PASSTHROUGH_CTX, help="Print `terraform output -json`.")
def output(ctx: typer.Context) -> None:
    commands.do_output(_config(ctx), slot=_slot_flag(ctx), extra=ctx.args)


@app.command(
    "import",
    context_settings=PASSTHROUGH_CTX,
    help=(
        "Import an existing resource into the state. Forwards the decrypted "
        "tfvars + saved env so the provider config resolves."
    ),
)
def import_cmd(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Target resource address.")],
    resource_id: Annotated[str, typer.Argument(metavar="ID", help="Provider-specific resource ID.")],
) -> None:
    commands.do_import(
        _config(ctx),
        slot=_slot_flag(ctx),
        address=address,
        resource_id=resource_id,
        extra=ctx.args,
    )


# ---- `state` subcommand group -------------------------------------------------

state_app = typer.Typer(
    name="state",
    help="Terraform state subcommands (list, mv, rm, pull, push, show, replace-provider, identities).",
    no_args_is_help=True,
)
app.add_typer(state_app, name="state")


@state_app.command("list", context_settings=PASSTHROUGH_CTX, help="List resources in the state.")
def state_list(
    ctx: typer.Context,
    addresses: Annotated[list[str] | None, typer.Argument(help="Resource addresses to filter on.")] = None,
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="list",
        args=addresses or [],
        extra=ctx.args,
    )


@state_app.command("show", context_settings=PASSTHROUGH_CTX, help="Show a resource in the state.")
def state_show(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Resource address to inspect.")],
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="show",
        args=[address],
        extra=ctx.args,
    )


@state_app.command("mv", context_settings=PASSTHROUGH_CTX, help="Move an item in the state.")
def state_mv(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source resource address.")],
    destination: Annotated[str, typer.Argument(help="Destination resource address.")],
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="mv",
        args=[source, destination],
        extra=ctx.args,
    )


@state_app.command("rm", context_settings=PASSTHROUGH_CTX, help="Remove instances from the state.")
def state_rm(
    ctx: typer.Context,
    addresses: Annotated[list[str], typer.Argument(help="Resource addresses to remove (≥1 required).")],
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="rm",
        args=addresses,
        extra=ctx.args,
    )


@state_app.command("pull", context_settings=PASSTHROUGH_CTX, help="Pull the current state to stdout.")
def state_pull(ctx: typer.Context) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="pull",
        args=[],
        extra=ctx.args,
    )


@state_app.command("push", context_settings=PASSTHROUGH_CTX, help="Update the remote state from a local file.")
def state_push(
    ctx: typer.Context,
    path: Annotated[
        pathlib.Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True, help="Local state file."),
    ],
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="push",
        args=[str(path)],
        extra=ctx.args,
    )


@state_app.command("replace-provider", context_settings=PASSTHROUGH_CTX, help="Replace provider in the state.")
def state_replace_provider(
    ctx: typer.Context,
    from_provider: Annotated[str, typer.Argument(metavar="FROM", help="Current provider address.")],
    to_provider: Annotated[str, typer.Argument(metavar="TO", help="Replacement provider address.")],
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="replace-provider",
        args=[from_provider, to_provider],
        extra=ctx.args,
    )


@state_app.command("identities", context_settings=PASSTHROUGH_CTX, help="List identities of resources in the state.")
def state_identities(
    ctx: typer.Context,
    addresses: Annotated[list[str] | None, typer.Argument(help="Resource addresses to filter on.")] = None,
) -> None:
    commands.do_state(
        _config(ctx),
        slot=_slot_flag(ctx),
        subcommand="identities",
        args=addresses or [],
        extra=ctx.args,
    )


@app.command("last", help="Print the last terraform invocation for the active slot.")
def last(ctx: typer.Context) -> None:
    import shlex

    try:
        payload = self_commands.do_last_invocation(_config(ctx), slot=_slot_flag(ctx))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"timestamp = {payload.get('timestamp', '(unknown)')}")
    exit_code = payload.get("exit_code")
    typer.echo(f"exit_code = {exit_code if exit_code is not None else '(unknown, e.g. exec passthrough)'}")
    argv = payload.get("argv") or []
    typer.echo(f"argv      = {' '.join(shlex.quote(a) for a in argv)}")


@app.command("status", help="One-line summary of the active slot.")
def status(ctx: typer.Context) -> None:
    report = commands.status_report(_config(ctx), slot=_slot_flag(ctx))
    if not report.initialized:
        typer.echo("Not initialized. Run `tfp use <slot>` first.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"slot        = {report.slot} (via {report.slot_source})")
    typer.echo(f"tfvars      = {report.tfvars}")
    typer.echo(f"source_root = {report.source_root}")
    typer.echo(f"backend_key = {report.backend_key or '(none)'}")
    typer.echo(f"env keys    = {', '.join(report.env_keys) or '(none)'}")
    typer.echo(f"plan        = {'ready' if report.plan_ready else 'absent'}")


# ---- `self` subcommand group --------------------------------------------------

self_app = typer.Typer(name="self", help="Manage tf_project itself.", no_args_is_help=True)
app.add_typer(self_app, name="self")

self_config_app = typer.Typer(name="config", help="Inspect the active configuration.", no_args_is_help=True)
self_app.add_typer(self_config_app, name="config")

self_state_app = typer.Typer(name="state", help="Inspect or reset saved per-slot state.", no_args_is_help=True)
self_app.add_typer(self_state_app, name="state")

self_banner_app = typer.Typer(name="banner", help="Inspect tfvars banners.", no_args_is_help=True)
self_app.add_typer(self_banner_app, name="banner")

self_lock_app = typer.Typer(
    name="lock",
    help="Inspect or break the remote-state lock. azurerm + s3 backends only.",
    no_args_is_help=True,
)
self_app.add_typer(self_lock_app, name="lock")

self_providers_app = typer.Typer(name="providers", help="Provider tooling.", no_args_is_help=True)
self_app.add_typer(self_providers_app, name="providers")


@self_app.command("init", help="Bootstrap a tf_project config in the current directory.")
def self_init() -> None:
    try:
        path = self_commands.do_self_init(pathlib.Path.cwd())
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Wrote tf_project config to {path}")


@self_config_app.command("print", help="Print the effective configuration.")
def self_config_print(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a human-readable listing.")] = False,
) -> None:
    typer.echo(self_commands.do_self_config_print(_config(ctx), as_json=as_json))


@self_config_app.command("path", help="Print the path of the config file in use.")
def self_config_path() -> None:
    typer.echo(str(self_commands.do_self_config_path(pathlib.Path.cwd())))


@self_state_app.command("show", help="Print the saved slot state as JSON.")
def self_state_show(
    ctx: typer.Context,
    all_slots: Annotated[bool, typer.Option("--all", help="Show state for every slot.")] = False,
) -> None:
    try:
        typer.echo(self_commands.do_self_state_show(_config(ctx), slot=_slot_flag(ctx), all_slots=all_slots))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@self_state_app.command("clear", help="Delete the saved slot state file (active slot, or all with --all).")
def self_state_clear(
    ctx: typer.Context,
    all_slots: Annotated[bool, typer.Option("--all", help="Clear state for every slot.")] = False,
) -> None:
    cfg = _config(ctx)
    try:
        removed = self_commands.do_self_state_clear(cfg, slot=_slot_flag(ctx), all_slots=all_slots)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not removed:
        typer.echo("No state file to remove.")
        return
    for p in removed:
        typer.echo(f"Removed {p}")


@self_app.command("doctor", help="Run environment sanity checks.")
def self_doctor(ctx: typer.Context) -> None:
    checks = self_commands.do_self_doctor(_config(ctx))
    width = max(len(c.name) for c in checks)
    failures = 0
    for check in checks:
        status = "OK  " if check.ok else "FAIL"
        typer.echo(f"[{status}] {check.name.ljust(width)}  {check.detail}")
        if not check.ok:
            failures += 1
    if failures:
        raise typer.Exit(code=1)


@self_app.command(
    "trace",
    help="Print the argv `tfp <subcommand>` would build, without invoking anything.",
)
def self_trace(
    ctx: typer.Context,
    subcommand: Annotated[
        str,
        typer.Argument(help="One of: use, init, plan, apply, refresh, destroy, output, import."),
    ],
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    import shlex

    try:
        argv = self_commands.do_self_trace(
            _config(ctx),
            subcommand=subcommand,
            slot=_slot_flag(ctx),
            targets=targets,
            replaces=replaces,
        )
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(" ".join(shlex.quote(a) for a in argv))


@self_banner_app.command("check", help="Parse and validate a tfvars banner.")
def self_banner_check(
    ctx: typer.Context,
    tfvars: Annotated[
        pathlib.Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            autocompletion=_complete_tfvars,
        ),
    ],
) -> None:
    summary = self_commands.do_self_banner_check(_config(ctx), tfvars=tfvars)
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


self_snapshot_app = typer.Typer(
    name="snapshot",
    help="Manage remote-tfstate snapshots (auto-saved before state-mutating ops).",
    no_args_is_help=True,
)
self_app.add_typer(self_snapshot_app, name="snapshot")


@self_snapshot_app.command(
    "create",
    help="Pull the remote tfstate to a local file via `terraform state pull`.",
)
def self_snapshot_create(
    ctx: typer.Context,
    dest: Annotated[
        pathlib.Path | None,
        typer.Option(
            "--dest", help="Output path. Defaults to <tmp_dir>/snapshots/<slot>/<UTC-timestamp>-manual.tfstate."
        ),
    ] = None,
) -> None:
    try:
        written = self_commands.do_self_snapshot(_config(ctx), slot=_slot_flag(ctx), dest=dest)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Wrote snapshot to {written}")


@self_snapshot_app.command("list", help="List snapshots for the slot, newest first.")
def self_snapshot_list(ctx: typer.Context) -> None:
    try:
        entries = self_commands.do_self_snapshot_list(_config(ctx), slot=_slot_flag(ctx))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not entries:
        typer.echo("(no snapshots)")
        return
    for entry in entries:
        typer.echo(f"{entry.path.name}\t{entry.size}\t{entry.path}")


@self_snapshot_app.command(
    "restore",
    help="Push a saved snapshot back to the remote backend via `terraform state push -force`.",
)
def self_snapshot_restore(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="Snapshot filename or stem. Defaults to the most recent."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    cfg = _config(ctx)
    slot = _slot_flag(ctx)
    try:
        entries = self_commands.do_self_snapshot_list(cfg, slot=slot)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if not entries:
        typer.echo("(no snapshots to restore)", err=True)
        raise typer.Exit(code=1)
    target_name = name or entries[0].path.name
    if not yes:
        typer.confirm(
            f"Restore snapshot {target_name!r} to the remote backend? This will OVERWRITE the live tfstate.",
            abort=True,
        )
    try:
        restored = self_commands.do_self_snapshot_restore(cfg, slot=slot, name=name)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Restored {restored}")


@self_providers_app.command(
    "lock",
    help="Run `terraform providers lock` across common platforms (linux amd64+arm64 plus the host's platform).",
)
def self_providers_lock(
    ctx: typer.Context,
    platforms: Annotated[
        list[str] | None,
        typer.Option("--platform", "-p", help="Override the default platform set (repeatable)."),
    ] = None,
) -> None:
    try:
        self_commands.do_self_providers_lock(_config(ctx), slot=_slot_flag(ctx), platforms=platforms)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@self_lock_app.command("status", help="Show the remote-state lock state (azurerm or s3+dynamodb).")
def self_lock_status(ctx: typer.Context) -> None:
    try:
        status = self_commands.do_self_lock_status(_config(ctx), slot=_slot_flag(ctx))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"backend        = {status.backend}")
    typer.echo(f"locked         = {status.locked}")
    typer.echo(f"detail         = {status.detail}")
    if status.lock_id:
        typer.echo(f"lock_id        = {status.lock_id}")
        typer.echo(f"lock_who       = {status.lock_who or '(unknown)'}")
        typer.echo(f"lock_operation = {status.lock_operation or '(unknown)'}")
        typer.echo(f"lock_created   = {status.lock_created or '(unknown)'}")
        typer.echo(f"\nTo release via terraform: tfp force-unlock {status.lock_id}")
    if status.locked:
        raise typer.Exit(code=2)


@self_lock_app.command(
    "break",
    help=(
        "Release the remote-state lock. Polite by default (terraform force-unlock); "
        "--blunt skips terraform and breaks the lease at the backend level."
    ),
)
def self_lock_break(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    blunt: Annotated[bool, typer.Option("--blunt", help="Skip the polite path.")] = False,
) -> None:
    if not yes:
        typer.confirm(
            "Release the remote-state lock? Only do this if terraform is no longer running.",
            abort=True,
        )
    try:
        result = self_commands.do_self_lock_break(_config(ctx), slot=_slot_flag(ctx), blunt=blunt)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if result.method == "force-unlock":
        typer.echo(f"Released via terraform force-unlock (lock_id={result.lock_id}).")
    else:
        typer.echo(f"Broke the {result.backend} lock at the backend level.")


# ---- Top-level dispatcher -----------------------------------------------------


def _strip_global_flags(argv: list[str]) -> tuple[list[str], bool, bool]:
    """Pull `--verbose` / `--dry-run` out of argv, stopping at the first `--`."""
    out: list[str] = []
    verbose = False
    dry_run = False
    seen_dash_dash = False
    for arg in argv:
        if arg == "--":
            seen_dash_dash = True
            out.append(arg)
            continue
        if not seen_dash_dash and arg == "--verbose":
            verbose = True
            continue
        if not seen_dash_dash and arg == "--dry-run":
            dry_run = True
            continue
        out.append(arg)
    return out, verbose, dry_run


def _split_passthrough(argv: list[str]) -> tuple[bool, list[str], str | None]:
    """Decide whether to passthrough to `terraform` and what to forward.

    Returns ``(is_passthrough, forwarded_args, slot_flag)``. Pulls
    ``-s/--slot <value>`` out of argv before scanning for the subcommand so
    the passthrough sees clean terraform argv, and so the slot flag is
    honoured by `do_passthrough` (env vars + -chdir come from the slot's
    saved state).
    """
    slot_flag: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-s", "--slot") and i + 1 < len(argv):
            slot_flag = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--slot="):
            slot_flag = arg.split("=", 1)[1]
            i += 1
            continue
        if arg.startswith("-"):
            i += 1
            continue
        if arg in WRAPPED_SUBCOMMANDS or arg == SELF_SUBCOMMAND:
            return (False, [], slot_flag)
        return (True, argv[i:], slot_flag)
    return (False, [], slot_flag)


def main() -> None:
    """Entry point: handle global flags, route, and translate errors to exit codes."""
    args, verbose, dry_run = _strip_global_flags(sys.argv[1:])

    # Best-effort: if there's already an active slot, point `tfp last`
    # recording at that slot's `last.json`. With no active slot (e.g. fresh
    # checkout, or pre-`tfp use` invocation), leave `last_path` unset so
    # the invocation simply isn't recorded.
    try:
        cfg_for_last = Config.discover()
    except ConfigError:
        # No config (e.g. `tfp self init`), or malformed config - skip
        # `last.json` routing here. The second `Config.discover()` inside
        # the try block below will re-raise and be caught by the funnel,
        # producing a clean exit-1 with the user-visible message.
        cfg_for_last = None

    last_path: pathlib.Path | None = None
    if cfg_for_last is not None:
        # Best-effort: route `last` recording into the active slot if any.
        active = slots.resolve_active(cfg_for_last, flag=None)
        if active is not None:
            last_path = cfg_for_last.last_invocation_for(active.name)
    terraform.set_runtime_options(dry_run=dry_run, verbose=verbose, last_invocation_path=last_path)

    try:
        is_passthrough, forwarded, passthrough_slot = _split_passthrough(args)
        if is_passthrough:
            cfg = Config.discover()
            migrate.run_if_needed(cfg)
            commands.do_passthrough(cfg, forwarded, slot=passthrough_slot)
            return
        sys.argv = [sys.argv[0], *args]
        app()
    except terraform.TerraformExit as exc:
        sys.exit(exc.code)
    except banner.BannerError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except banner.ProjectInfoNotFoundError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except commands.NoActiveSlotError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except commands.StateNotInitializedError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except commands.StaleTfplanError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except preflight.RecoveryError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except slots.SlotConflictError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except slots.SlotError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except LockBusyError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
