"""Typer CLI for tf_project."""

from __future__ import annotations

import pathlib
from typing import Annotated

import typer

from tf_project import commands
from tf_project.__version__ import __version__
from tf_project.config import Config

app = typer.Typer(
    name="tf-project",
    help="Custom Terraform project wrapper.",
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
def main(
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


@app.command("init", help="Initialize Terraform backend for a given tfvars file.")
def init(
    ctx: typer.Context,
    tfvars: Annotated[
        pathlib.Path,
        typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
    ],
) -> None:
    commands.do_init(_config(ctx), tfvars=tfvars)


@app.command("plan", help="Run `terraform plan` against the initialized project.")
def plan(
    ctx: typer.Context,
    targets: TargetsOption = None,
    replaces: ReplacesOption = None,
) -> None:
    commands.do_plan(_config(ctx), targets=targets, replaces=replaces)


@app.command("apply", help="Apply the saved tfplan.")
def apply(ctx: typer.Context) -> None:
    commands.do_apply(_config(ctx))


@app.command("refresh", help="Apply directly (without a saved plan), optionally targeted.")
def refresh(
    ctx: typer.Context,
    targets: TargetsOption = None,
) -> None:
    commands.do_refresh(_config(ctx), targets=targets)


@app.command("destroy", help="Run `terraform destroy`, optionally targeted.")
def destroy(
    ctx: typer.Context,
    targets: TargetsOption = None,
) -> None:
    commands.do_destroy(_config(ctx), targets=targets)


@app.command("fmt", help="Recursively `terraform fmt` the terraform/ and tfvars/ trees.")
def fmt(ctx: typer.Context) -> None:
    commands.do_fmt(_config(ctx))


@app.command("output", help="Print `terraform output -json`.")
def output(ctx: typer.Context) -> None:
    commands.do_output(_config(ctx))


@app.command("state-mv", help="Move a resource in the Terraform state.")
def state_mv(
    ctx: typer.Context,
    source: Annotated[str, typer.Argument(help="Source resource address")],
    destination: Annotated[str, typer.Argument(help="Destination resource address")],
) -> None:
    commands.do_state_mv(_config(ctx), source=source, destination=destination)
