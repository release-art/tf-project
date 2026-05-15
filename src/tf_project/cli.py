"""Typer CLI for tf_project."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Annotated

import click
import typer
from click.shell_completion import CompletionItem

from tf_project import banner, commands, self_commands, terraform
from tf_project.__version__ import __version__
from tf_project.commands import WRAPPED_SUBCOMMANDS
from tf_project.config import Config, ConfigError
from tf_project.state import LockBusyError, MyState

SELF_SUBCOMMAND = "self"
GLOBAL_FLAGS = {"--verbose", "--dry-run"}

PASSTHROUGH_CTX = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}

app = typer.Typer(
    name="tf-project",
    help=(
        "Custom Terraform project wrapper. Unknown subcommands are forwarded "
        "to `terraform` verbatim. Global flags: --verbose (echo terraform "
        "argv to stderr), --dry-run (print argv and skip execution)."
    ),
    no_args_is_help=True,
)


def _config(ctx: typer.Context) -> Config:
    if not isinstance(ctx.obj, Config):
        ctx.obj = Config.discover()
    return ctx.obj


def _complete_tfvars(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[CompletionItem]:
    """Tab-completion: list tfvars files under config.tfvars_dir, labelled by project."""
    try:
        cfg = Config.discover()
    except Exception:  # noqa: BLE001 — no config yet → no completions
        return []
    out: list[CompletionItem] = []
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
            info = banner.find_project_info(path)
            label = info.get("project") or "(no project)"
        except (banner.ProjectInfoNotFoundError, OSError):
            label = "(no banner)"
        out.append(CompletionItem(value=display, help=f"project={label}"))
    return out


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version and exit."),
    ] = False,
) -> None:
    """Custom Terraform project wrapper."""


TargetsOption = Annotated[
    list[str] | None,
    typer.Option(
        "--target",
        "-t",
        metavar="RESOURCE",
        help="Limit operation to a specific resource address (repeatable).",
    ),
]
ReplacesOption = Annotated[
    list[str] | None,
    typer.Option(
        "--replace",
        "-r",
        metavar="RESOURCE",
        help="Force replacement of a specific resource address (repeatable).",
    ),
]


@app.command("init", context_settings=PASSTHROUGH_CTX, help="Initialize Terraform backend for a given tfvars file.")
def init(
    ctx: typer.Context,
    tfvars: Annotated[
        pathlib.Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            shell_complete=_complete_tfvars,
        ),
    ],
    force: Annotated[bool, typer.Option("--force", help="Skip the tfvars-switch confirmation.")] = False,
) -> None:
    cfg = _config(ctx)
    old = MyState.load(cfg)
    if old is not None and pathlib.Path(old.tfvars).resolve() != tfvars.resolve() and not force:
        typer.confirm(
            f"Switching saved init from {old.tfvars} → {tfvars}. Continue?",
            abort=True,
        )
    commands.do_init(cfg, tfvars=tfvars, extra=ctx.args)


@app.command("plan", context_settings=PASSTHROUGH_CTX, help="Run `terraform plan` against the initialized project.")
def plan(
    ctx: typer.Context,
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    commands.do_plan(_config(ctx), targets=targets, replaces=replaces, extra=ctx.args)


@app.command("apply", context_settings=PASSTHROUGH_CTX, help="Apply the saved tfplan.")
def apply(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option("--force", help="Apply even if the tfvars changed since the plan.")] = False,
) -> None:
    commands.do_apply(_config(ctx), force=force, extra=ctx.args)


@app.command(
    "refresh",
    context_settings=PASSTHROUGH_CTX,
    help="Apply directly (without a saved plan), optionally targeted.",
)
def refresh(
    ctx: typer.Context,
    targets: TargetsOption = None,
) -> None:
    commands.do_refresh(_config(ctx), targets=targets, extra=ctx.args)


@app.command("destroy", context_settings=PASSTHROUGH_CTX, help="Run `terraform destroy`, optionally targeted.")
def destroy(
    ctx: typer.Context,
    targets: TargetsOption = None,
) -> None:
    commands.do_destroy(_config(ctx), targets=targets, extra=ctx.args)


@app.command(
    "fmt",
    context_settings=PASSTHROUGH_CTX,
    help="Recursively `terraform fmt` the terraform/ and tfvars/ trees.",
)
def fmt(ctx: typer.Context) -> None:
    commands.do_fmt(_config(ctx), extra=ctx.args)


@app.command("output", context_settings=PASSTHROUGH_CTX, help="Print `terraform output -json`.")
def output(ctx: typer.Context) -> None:
    commands.do_output(_config(ctx), extra=ctx.args)


@app.command(
    "import",
    context_settings=PASSTHROUGH_CTX,
    help=(
        "Import an existing resource into the Terraform state. Forwards "
        "the decrypted tfvars + saved env so the provider config resolves."
    ),
)
def import_cmd(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Target resource address (e.g. `aws_s3_bucket.foo`).")],
    resource_id: Annotated[str, typer.Argument(metavar="ID", help="Provider-specific ID of the existing resource.")],
) -> None:
    commands.do_import(_config(ctx), address=address, resource_id=resource_id, extra=ctx.args)


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
    addresses: Annotated[
        list[str] | None,
        typer.Argument(help="Resource addresses to filter on. Omit to list everything."),
    ] = None,
) -> None:
    commands.do_state(_config(ctx), subcommand="list", args=addresses or [], extra=ctx.args)


@state_app.command("show", context_settings=PASSTHROUGH_CTX, help="Show a resource in the state.")
def state_show(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Resource address to inspect.")],
) -> None:
    commands.do_state(_config(ctx), subcommand="show", args=[address], extra=ctx.args)


@state_app.command("mv", context_settings=PASSTHROUGH_CTX, help="Move an item in the state.")
def state_mv(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source resource address.")],
    destination: Annotated[str, typer.Argument(help="Destination resource address.")],
) -> None:
    commands.do_state(_config(ctx), subcommand="mv", args=[source, destination], extra=ctx.args)


@state_app.command("rm", context_settings=PASSTHROUGH_CTX, help="Remove instances from the state.")
def state_rm(
    ctx: typer.Context,
    addresses: Annotated[
        list[str],
        typer.Argument(help="Resource addresses to remove (at least one required)."),
    ],
) -> None:
    commands.do_state(_config(ctx), subcommand="rm", args=addresses, extra=ctx.args)


@state_app.command("pull", context_settings=PASSTHROUGH_CTX, help="Pull the current state and write it to stdout.")
def state_pull(ctx: typer.Context) -> None:
    commands.do_state(_config(ctx), subcommand="pull", args=[], extra=ctx.args)


@state_app.command("push", context_settings=PASSTHROUGH_CTX, help="Update the remote state from a local state file.")
def state_push(
    ctx: typer.Context,
    path: Annotated[
        pathlib.Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True, help="Local state file."),
    ],
) -> None:
    commands.do_state(_config(ctx), subcommand="push", args=[str(path)], extra=ctx.args)


@state_app.command("replace-provider", context_settings=PASSTHROUGH_CTX, help="Replace provider in the state.")
def state_replace_provider(
    ctx: typer.Context,
    from_provider: Annotated[str, typer.Argument(metavar="FROM", help="Current provider address.")],
    to_provider: Annotated[str, typer.Argument(metavar="TO", help="Replacement provider address.")],
) -> None:
    commands.do_state(
        _config(ctx),
        subcommand="replace-provider",
        args=[from_provider, to_provider],
        extra=ctx.args,
    )


@state_app.command(
    "identities", context_settings=PASSTHROUGH_CTX, help="List the identities of resources in the state."
)
def state_identities(
    ctx: typer.Context,
    addresses: Annotated[
        list[str] | None,
        typer.Argument(help="Resource addresses to filter on. Omit for all."),
    ] = None,
) -> None:
    commands.do_state(_config(ctx), subcommand="identities", args=addresses or [], extra=ctx.args)


@app.command("last", help="Print the last terraform invocation recorded by tfp (argv + exit code).")
def last(ctx: typer.Context) -> None:
    try:
        payload = self_commands.do_last_invocation(_config(ctx))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"timestamp = {payload.get('timestamp', '(unknown)')}")
    exit_code = payload.get("exit_code")
    typer.echo(f"exit_code = {exit_code if exit_code is not None else '(unknown, e.g. exec passthrough)'}")
    argv = payload.get("argv") or []
    import shlex

    typer.echo(f"argv      = {' '.join(shlex.quote(a) for a in argv)}")


@app.command("status", help="Print a one-line summary of the current init state.")
def status(ctx: typer.Context) -> None:
    report = commands.status_report(_config(ctx))
    if not report.initialized:
        typer.echo("Not initialized. Run `tfp init <tfvars>` first.", err=True)
        raise typer.Exit(code=1)
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

self_state_app = typer.Typer(name="state", help="Inspect or reset the saved init state.", no_args_is_help=True)
self_app.add_typer(self_state_app, name="state")

self_banner_app = typer.Typer(name="banner", help="Inspect tfvars banners.", no_args_is_help=True)
self_app.add_typer(self_banner_app, name="banner")

self_lock_app = typer.Typer(
    name="lock",
    help="Inspect or break the remote-state lock. Azure backend only.",
    no_args_is_help=True,
)
self_app.add_typer(self_lock_app, name="lock")


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


@self_state_app.command("show", help="Print the saved init state as JSON.")
def self_state_show(ctx: typer.Context) -> None:
    try:
        typer.echo(self_commands.do_self_state_show(_config(ctx)))
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@self_state_app.command("clear", help="Delete the saved init state file.")
def self_state_clear(ctx: typer.Context) -> None:
    cfg = _config(ctx)
    removed = self_commands.do_self_state_clear(cfg)
    if removed:
        typer.echo(f"Removed {cfg.state_file}")
    else:
        typer.echo(f"No state file at {cfg.state_file} — nothing to do.")


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
        typer.Argument(help="One of: init, plan, apply, refresh, destroy, output."),
    ],
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    import shlex

    try:
        argv = self_commands.do_self_trace(
            _config(ctx),
            subcommand=subcommand,
            targets=targets,
            replaces=replaces,
        )
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(" ".join(shlex.quote(a) for a in argv))


@self_banner_app.command("check", help="Parse and validate a tfvars banner without running terraform.")
def self_banner_check(
    ctx: typer.Context,
    tfvars: Annotated[
        pathlib.Path,
        typer.Argument(
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            shell_complete=_complete_tfvars,
        ),
    ],
) -> None:
    summary = self_commands.do_self_banner_check(_config(ctx), tfvars=tfvars)
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


@self_app.command("snapshot", help="Pull the remote tfstate to a local file via `terraform state pull`.")
def self_snapshot(
    ctx: typer.Context,
    dest: Annotated[
        pathlib.Path | None,
        typer.Option("--dest", help="Output path. Defaults to <tmp_dir>/snapshot-<UTC-timestamp>.tfstate."),
    ] = None,
) -> None:
    try:
        written = self_commands.do_self_snapshot(_config(ctx), dest=dest)
    except self_commands.SelfCommandError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Wrote snapshot to {written}")


@self_lock_app.command("status", help="Show the remote-state lock state (azurerm or s3+dynamodb).")
def self_lock_status(ctx: typer.Context) -> None:
    try:
        status = self_commands.do_self_lock_status(_config(ctx))
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
        "pass --blunt to skip terraform and break the lease at the backend level."
    ),
)
def self_lock_break(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    blunt: Annotated[
        bool,
        typer.Option(
            "--blunt",
            help="Skip the polite `terraform force-unlock` path; break the lease directly.",
        ),
    ] = False,
) -> None:
    if not yes:
        typer.confirm(
            "Release the remote-state lock? Only do this if terraform is no longer running.",
            abort=True,
        )
    try:
        result = self_commands.do_self_lock_break(_config(ctx), blunt=blunt)
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


def _split_passthrough(argv: list[str]) -> tuple[bool, list[str]]:
    """Decide whether to passthrough to `terraform` and, if so, what to forward."""
    for i, arg in enumerate(argv):
        if arg.startswith("-"):
            continue
        if arg in WRAPPED_SUBCOMMANDS or arg == SELF_SUBCOMMAND:
            return (False, [])
        return (True, argv[i:])
    return (False, [])


def main() -> None:
    """Entry point: handle global flags, route, and translate errors to exit codes."""
    args, verbose, dry_run = _strip_global_flags(sys.argv[1:])

    # Best-effort: hand terraform.run a path for `tfp last` recording.
    last_path: pathlib.Path | None = None
    try:
        last_path = Config.discover().tmp_dir / "last.json"
    except Exception:  # noqa: BLE001 — config not present yet (e.g., `tfp self init`)
        pass
    terraform.set_runtime_options(dry_run=dry_run, verbose=verbose, last_invocation_path=last_path)

    try:
        is_passthrough, forwarded = _split_passthrough(args)
        if is_passthrough:
            cfg = Config.discover()
            commands.do_passthrough(cfg, forwarded)
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
    except commands.StateNotInitializedError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except commands.StaleTfplanError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except LockBusyError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
