from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from tf_project.cli import _split_passthrough, app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_top_level_help(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "tf-project" in result.stdout


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


@pytest.mark.parametrize(
    "subcommand",
    ["init", "plan", "apply", "refresh", "destroy", "fmt", "output", "state-mv", "self"],
)
def test_subcommand_help(runner: CliRunner, subcommand: str) -> None:
    result = runner.invoke(app, [subcommand, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "subcommand",
    [
        "self init",
        "self doctor",
        "self config",
        "self config print",
        "self config path",
        "self state",
        "self state show",
        "self state clear",
    ],
)
def test_self_subcommand_help(runner: CliRunner, subcommand: str) -> None:
    result = runner.invoke(app, [*subcommand.split(), "--help"])
    assert result.exit_code == 0, result.stdout


def test_cli_dispatches_init(
    runner: CliRunner,
    project_tree: pathlib.Path,
    tfvars: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project_tree / "tf_project.toml").write_text(
        "[tf_project]\n"
        'terraform_dir = "terraform"\n'
        'tfvars_dir = "tfvars"\n'
        'tmp_dir = "tmp"\n'
        'state_key_prefix = "terraform/azure/"\n\n'
        "[tf_project.secrets]\n"
        "command = []\n"
    )
    monkeypatch.chdir(project_tree)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.run",
        lambda cmd, env=None: calls.append(cmd),
    )
    result = runner.invoke(app, ["init", str(tfvars)])
    assert result.exit_code == 0, result.stdout
    assert calls and pathlib.Path(calls[0][0]).name == "terraform"


@pytest.mark.parametrize(
    ("argv", "expected_passthrough", "expected_args"),
    [
        (["plan"], False, []),
        (["init", "foo.tfvars"], False, []),
        (["--version"], False, []),
        (["--help"], False, []),
        (["validate"], True, ["validate"]),
        (["validate", "-json"], True, ["validate", "-json"]),
        (["workspace", "list"], True, ["workspace", "list"]),
        (["--something", "validate", "-json"], True, ["validate", "-json"]),
    ],
)
def test_split_passthrough(argv: list[str], expected_passthrough: bool, expected_args: list[str]) -> None:
    is_passthrough, forwarded = _split_passthrough(argv)
    assert is_passthrough is expected_passthrough
    assert forwarded == expected_args
