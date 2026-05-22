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
    [
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
        "self",
    ],
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
        "self banner",
        "self banner check",
        "self lock",
        "self lock status",
        "self lock break",
        "self providers",
        "self providers lock",
    ],
)
def test_self_subcommand_help(runner: CliRunner, subcommand: str) -> None:
    result = runner.invoke(app, [*subcommand.split(), "--help"])
    assert result.exit_code == 0, result.stdout


@pytest.mark.parametrize(
    "subcommand",
    [
        "state list",
        "state show",
        "state mv",
        "state rm",
        "state pull",
        "state push",
        "state replace-provider",
        "state identities",
    ],
)
def test_state_subcommand_help(runner: CliRunner, subcommand: str) -> None:
    result = runner.invoke(app, [*subcommand.split(), "--help"])
    assert result.exit_code == 0, result.stdout


def test_cli_dispatches_use(
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
    result = runner.invoke(app, ["use", "dev"])
    assert result.exit_code == 0, result.stdout
    assert calls and pathlib.Path(calls[0][0]).name == "terraform"


@pytest.mark.parametrize(
    ("argv", "expected_passthrough", "expected_args"),
    [
        (["plan"], False, []),
        (["use", "dev"], False, []),
        (["--version"], False, []),
        (["--help"], False, []),
        (["status"], False, []),
        (["self", "doctor"], False, []),
        (["import", "a", "b"], False, []),
        (["state", "rm", "x"], False, []),
        (["validate"], True, ["validate"]),
        (["validate", "-json"], True, ["validate", "-json"]),
        (["workspace", "list"], True, ["workspace", "list"]),
        (["--something", "validate", "-json"], True, ["validate", "-json"]),
        (["-s", "prod", "plan"], False, []),
        (["--slot=prod", "validate"], True, ["validate"]),
    ],
)
def test_split_passthrough(argv: list[str], expected_passthrough: bool, expected_args: list[str]) -> None:
    is_passthrough, forwarded, _slot = _split_passthrough(argv)
    assert is_passthrough is expected_passthrough
    assert forwarded == expected_args


def test_split_passthrough_extracts_slot_flag() -> None:
    is_passthrough, forwarded, slot = _split_passthrough(["-s", "prod", "validate"])
    assert is_passthrough is True
    assert forwarded == ["validate"]
    assert slot == "prod"


def test_split_passthrough_extracts_slot_equals() -> None:
    is_passthrough, forwarded, slot = _split_passthrough(["--slot=prod", "validate"])
    assert is_passthrough is True
    assert forwarded == ["validate"]
    assert slot == "prod"


@pytest.mark.parametrize(
    ("argv", "want_args", "want_verbose", "want_dry_run"),
    [
        (["plan"], ["plan"], False, False),
        (["--verbose", "plan"], ["plan"], True, False),
        (["--dry-run", "plan"], ["plan"], False, True),
        (["--verbose", "--dry-run", "plan"], ["plan"], True, True),
        (["plan", "--", "--verbose"], ["plan", "--", "--verbose"], False, False),
        (["plan", "--verbose"], ["plan"], True, False),
    ],
)
def test_strip_global_flags(argv: list[str], want_args: list[str], want_verbose: bool, want_dry_run: bool) -> None:
    from tf_project.cli import _strip_global_flags

    args, verbose, dry_run = _strip_global_flags(argv)
    assert args == want_args
    assert verbose is want_verbose
    assert dry_run is want_dry_run


def test_complete_tfvars_lists_banner_projects(project_tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    (project_tree / "tf_project.toml").write_text(
        "[tf_project]\n"
        'terraform_dir = "terraform"\n'
        'tfvars_dir = "tfvars"\n'
        'tmp_dir = "tmp"\n'
        'state_key_prefix = ""\n\n'
        "[tf_project.secrets]\n"
        "command = []\n"
    )
    (project_tree / "tfvars" / "prod.tfvars").write_text(
        f'# {json.dumps({"header": "terraform", "project": "prod-app"})}\nfoo = "bar"\n'
    )
    monkeypatch.chdir(project_tree)
    from tf_project.cli import _complete_tfvars

    items = _complete_tfvars(None, None, "tfvars/")  # type: ignore[arg-type]
    values = {it.value for it in items}
    helps = {it.value: it.help for it in items}
    assert "tfvars/dev.tfvars" in values
    assert "tfvars/prod.tfvars" in values
    assert "project=demo" in (helps["tfvars/dev.tfvars"] or "")
    assert "project=prod-app" in (helps["tfvars/prod.tfvars"] or "")


def test_ls_warns_on_slot_conflict(
    runner: CliRunner, project_tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    (project_tree / "tf_project.toml").write_text(
        "[tf_project]\n"
        'terraform_dir = "terraform"\n'
        'tfvars_dir = "tfvars"\n'
        'tmp_dir = "tmp"\n\n'
        "[tf_project.secrets]\n"
        "command = []\n"
    )
    # Two tfvars claim the same slot name "dev".
    (project_tree / "tfvars" / "dev-old.tfvars").write_text(
        f'# {json.dumps({"header": "terraform", "project": "demo", "slot": "dev"})}\nfoo="bar"\n'
    )
    monkeypatch.chdir(project_tree)
    result = runner.invoke(app, ["ls"])
    # ls itself succeeds; conflict warning goes to stderr.
    assert "warning" in result.output or "warning" in (result.stderr or "")


def test_complete_slot_lists_slot_names(project_tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    (project_tree / "tf_project.toml").write_text(
        "[tf_project]\n"
        'terraform_dir = "terraform"\n'
        'tfvars_dir = "tfvars"\n'
        'tmp_dir = "tmp"\n'
        'state_key_prefix = ""\n\n'
        "[tf_project.secrets]\n"
        "command = []\n"
    )
    (project_tree / "tfvars" / "prod.tfvars").write_text(
        f'# {json.dumps({"header": "terraform", "project": "demo", "slot": "production"})}\nfoo = "bar"\n'
    )
    monkeypatch.chdir(project_tree)
    from tf_project.cli import _complete_slot

    items = _complete_slot(None, None, "")  # type: ignore[arg-type]
    names = {it.value for it in items}
    assert "dev" in names
    assert "production" in names
