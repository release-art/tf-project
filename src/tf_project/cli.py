"""Typer CLI for tf_project."""

from __future__ import annotations

import pathlib
import sys
from typing import Annotated

import typer

from tf_project import commands, self_commands
from tf_project.__version__ import __version__
from tf_project.commands import WRAPPED_SUBCOMMANDS
from tf_project.config import Config

# Top-level group names we own; anything else falls through to terraform.
SELF_SUBCOMMAND = "self"

# Subcommand context settings: let terraform-style flags (`-foo=bar`) flow
# through unparsed so users can mix our convenience options with raw
# terraform CLI flags on the same line.
PASSTHROUGH_CTX = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
}

app = typer.Typer(
    name="tf-project",
    help="Custom Terraform project wrapper. Unknown subcommands are forwarded to `terraform` verbatim.",
    no_args_is_help=True,
    add_completion=False,
)


def _config(ctx: typer.Context) -> Config:
    if not isinstance(ctx.obj, Config):
        ctx.obj = Config.discover()
    return ctx.obj


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
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
) -> None:
    commands.do_init(_config(ctx), tfvars=tfvars, extra=ctx.args)


@app.command("plan", context_settings=PASSTHROUGH_CTX, help="Run `terraform plan` against the initialized project.")
def plan(
    ctx: typer.Context,
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    commands.do_plan(_config(ctx), targets=targets, replaces=replaces, extra=ctx.args)


@app.command("apply", context_settings=PASSTHROUGH_CTX, help="Apply the saved tfplan.")
def apply(ctx: typer.Context) -> None:
    commands.do_apply(_config(ctx), extra=ctx.args)


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


@app.command("state-mv", context_settings=PASSTHROUGH_CTX, help="Move a resource in the Terraform state.")
def state_mv(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source resource address")],
    destination: Annotated[str, typer.Argument(help="Destination resource address")],
) -> None:
    commands.do_state_mv(_config(ctx), source=source, destination=destination, extra=ctx.args)


self_app = typer.Typer(name="self", help="Manage tf_project itself.", no_args_is_help=True)
app.add_typer(self_app, name="self")

self_config_app = typer.Typer(name="config", help="Inspect the active configuration.", no_args_is_help=True)
self_app.add_typer(self_config_app, name="config")

self_state_app = typer.Typer(name="state", help="Inspect or reset the saved init state.", no_args_is_help=True)
self_app.add_typer(self_state_app, name="state")


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


def _split_passthrough(argv: list[str]) -> tuple[bool, list[str]]:
    """Decide whether to passthrough to `terraform` and, if so, what to forward.

    Returns (is_passthrough, terraform_args). Any leading flags (e.g.
    `--version`, `--help`) keep us in the Typer app.
    """
    for i, arg in enumerate(argv):
        if arg.startswith("-"):
            continue
        if arg in WRAPPED_SUBCOMMANDS or arg == SELF_SUBCOMMAND:
            return (False, [])
        return (True, argv[i:])
    return (False, [])


def main() -> None:
    """Entry point: dispatch unknown subcommands straight to `terraform`."""
    is_passthrough, forwarded = _split_passthrough(sys.argv[1:])
    if is_passthrough:
        cfg = Config.discover()
        commands.do_passthrough(cfg, forwarded)
        return
    app()


if __name__ == "__main__":
    main()
