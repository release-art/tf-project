"""Tests for `cli.main()` - the top-level dispatcher and error funnel.

These tests invoke `main()` directly with a monkey-patched `sys.argv` and
assert the resulting `SystemExit` code. They exercise the long error-catch
chain at the bottom of `main()` and the passthrough branch (which goes via
`os.execvpe`, mocked here).
"""

from __future__ import annotations

import pathlib

import pytest

from tf_project import cli


@pytest.fixture
def in_project(project_tree: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:  # type: ignore[no-untyped-def]
    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    return project_tree


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Invoke cli.main() with the given argv (just args, no program name).

    Returns the SystemExit code (0 if no exit was raised - typer.Exit(0)
    on success surfaces as SystemExit(0)).
    """
    monkeypatch.setattr("sys.argv", ["tfp", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_main_passthrough_invokes_exec(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown subcommand → `os.execvpe` (mocked); no SystemExit."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.exec_passthrough",
        lambda cmd, env=None: calls.append(cmd),
    )
    code = _run_main(monkeypatch, ["validate"])
    assert code == 0
    assert calls and calls[0][-1] == "validate"


def test_main_passthrough_with_slot_flag(
    in_project: pathlib.Path,
    tfvars: pathlib.Path,
    save_dev_state,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    """`tfp -s dev <unknown>` routes -chdir + env from the dev slot."""
    save_dev_state()
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.exec_passthrough",
        lambda cmd, env=None: calls.append({"cmd": cmd, "env": env}),
    )
    code = _run_main(monkeypatch, ["-s", "dev", "validate"])
    assert code == 0
    cmd = calls[0]["cmd"]
    assert any(c.startswith("-chdir=") for c in cmd)


def test_main_wrapped_command_succeeds(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tf_project.cli.commands.do_fmt", lambda config, extra=None: None)
    code = _run_main(monkeypatch, ["fmt"])
    assert code == 0


def test_main_translates_terraform_exit(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project import terraform as terraform_mod

    def boom(*a: object, **kw: object) -> None:
        raise terraform_mod.TerraformExit(2)

    monkeypatch.setattr("tf_project.cli.commands.do_fmt", boom)
    code = _run_main(monkeypatch, ["fmt"])
    assert code == 2


def test_main_translates_banner_error(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.banner import BannerError

    def boom(*a: object, **kw: object) -> None:
        raise BannerError(pathlib.Path("/x.tfvars"), "bad")

    monkeypatch.setattr("tf_project.cli.commands.do_use", boom)
    code = _run_main(monkeypatch, ["use", "dev"])
    assert code == 1


def test_main_translates_project_info_not_found(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.banner import ProjectInfoNotFoundError

    def boom(*a: object, **kw: object) -> None:
        raise ProjectInfoNotFoundError("no banner")

    monkeypatch.setattr("tf_project.cli.commands.do_use", boom)
    code = _run_main(monkeypatch, ["use", "dev"])
    assert code == 1


def test_main_translates_no_active_slot(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan without an active slot surfaces NoActiveSlotError via main()."""
    code = _run_main(monkeypatch, ["plan"])
    assert code == 1


def test_main_translates_state_not_initialized(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.commands import StateNotInitializedError

    def boom(*a: object, **kw: object) -> None:
        raise StateNotInitializedError("no state")

    monkeypatch.setattr("tf_project.cli.commands.do_plan", boom)
    code = _run_main(monkeypatch, ["plan"])
    assert code == 1


def test_main_translates_stale_tfplan(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.commands import StaleTfplanError

    def boom(*a: object, **kw: object) -> None:
        raise StaleTfplanError("stale")

    monkeypatch.setattr("tf_project.cli.commands.do_apply", boom)
    code = _run_main(monkeypatch, ["apply"])
    assert code == 1


def test_main_translates_lock_busy(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.state import LockBusyError

    def boom(*a: object, **kw: object) -> None:
        raise LockBusyError("busy")

    monkeypatch.setattr("tf_project.cli.commands.do_apply", boom)
    code = _run_main(monkeypatch, ["apply"])
    assert code == 1


def test_main_translates_slot_conflict(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SlotConflictError raised inside a typer command's `try` is also caught at main()."""
    from tf_project import slots as slots_mod
    from tf_project.slots import ResolvedSlot

    a_slot = ResolvedSlot(
        name="x",
        name_is_explicit=True,
        tfvars=pathlib.Path("/a"),
        project="demo",
        source_root=pathlib.Path("/x"),
        env={},
        backend_config={},
        slot_dir=pathlib.Path("/y/x"),
    )
    b_slot = a_slot.model_copy(update={"tfvars": pathlib.Path("/b")})

    def boom(*a: object, **kw: object) -> None:
        raise slots_mod.SlotConflictError(a_slot, b_slot)

    # Use a command path that doesn't already catch SlotConflictError locally:
    # `tfp plan` re-raises via main()'s `except slots.SlotConflictError`.
    monkeypatch.setattr("tf_project.cli.commands.do_plan", boom)
    code = _run_main(monkeypatch, ["plan"])
    assert code == 1


def test_main_translates_slot_error(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project import slots as slots_mod

    def boom(*a: object, **kw: object) -> None:
        raise slots_mod.SlotError("nope")

    monkeypatch.setattr("tf_project.cli.commands.do_plan", boom)
    code = _run_main(monkeypatch, ["plan"])
    assert code == 1


def test_main_translates_config_error(project_tree: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed tf_project.toml → ConfigError → exit 1 via main()."""
    (project_tree / "tf_project.toml").write_text("[tf_project]\nterraform_dir = 123\n")
    monkeypatch.chdir(project_tree)
    code = _run_main(monkeypatch, ["plan"])
    assert code == 1


def test_main_no_config_at_all(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No tf_project.toml anywhere → ConfigNotFoundError swallowed by main, app surfaces it."""
    monkeypatch.chdir(tmp_path)
    code = _run_main(monkeypatch, ["plan"])
    # ConfigNotFoundError is a ConfigError subclass - main catches and exits 1.
    assert code == 1


def test_main_routes_last_path_when_slot_active(
    in_project: pathlib.Path,
    tfvars: pathlib.Path,
    save_dev_state,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When an active slot exists at startup, `terraform.set_runtime_options` gets a per-slot last.json."""
    save_dev_state()
    captured: dict[str, object] = {}

    def fake_set(**kw: object) -> None:
        captured.update(kw)

    monkeypatch.setattr("tf_project.terraform.set_runtime_options", fake_set)
    monkeypatch.setattr("tf_project.cli.commands.do_fmt", lambda config, extra=None: None)
    _run_main(monkeypatch, ["fmt"])
    assert captured["last_invocation_path"] is not None
    assert "slots/dev/last.json" in str(captured["last_invocation_path"])


# ---- completion functions ---------------------------------------------------


def test_complete_tfvars_returns_empty_without_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    items = cli._complete_tfvars(None, None, "")  # type: ignore[arg-type]
    assert items == []


def test_complete_tfvars_skips_missing_dir(
    project_tree: pathlib.Path,
    write_tfp_toml,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    write_tfp_toml(project_tree)
    # Remove tfvars_dir so the is_dir() guard returns []
    import shutil

    shutil.rmtree(project_tree / "tfvars")
    monkeypatch.chdir(project_tree)
    items = cli._complete_tfvars(None, None, "")  # type: ignore[arg-type]
    assert items == []


def test_complete_tfvars_handles_unrelated_cwd(
    project_tree: pathlib.Path,
    write_tfp_toml,  # type: ignore[no-untyped-def]
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a tfvars file isn't relative to cwd, completion falls back to absolute paths."""
    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    # Point Config.discover at project_tree, but pretend cwd is somewhere outside.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    # Patch Config.discover so it returns the configured project even though cwd is elsewhere.
    from tf_project.config import Config

    cfg = Config.discover(project_tree)
    monkeypatch.setattr("tf_project.cli.Config.discover", classmethod(lambda cls, start=None: cfg))
    monkeypatch.chdir(outside)
    items = cli._complete_tfvars(None, None, "")  # type: ignore[arg-type]
    # All paths should be absolute (couldn't relative_to cwd).
    assert all(it.value.startswith("/") for it in items)


def test_complete_tfvars_handles_no_banner(
    project_tree: pathlib.Path,
    write_tfp_toml,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    """A banner-less tfvars file is listed with `project=(no banner)`."""
    write_tfp_toml(project_tree)
    (project_tree / "tfvars" / "naked.tfvars").write_text('foo = "bar"\n')  # no banner
    monkeypatch.chdir(project_tree)
    items = cli._complete_tfvars(None, None, "")  # type: ignore[arg-type]
    helps = {it.value: it.help for it in items}
    naked = next(v for v in helps if v.endswith("naked.tfvars"))
    assert "(no banner)" in (helps[naked] or "")


def test_complete_slot_returns_empty_without_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    items = cli._complete_slot(None, None, "")  # type: ignore[arg-type]
    assert items == []


def test_complete_use_arg_combines_both(
    project_tree: pathlib.Path,
    write_tfp_toml,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    items = cli._complete_use_arg(None, None, "")  # type: ignore[arg-type]
    values = {it.value for it in items}
    assert "dev" in values  # slot name
    assert any(v.endswith("dev.tfvars") for v in values)  # tfvars path


def test_self_lock_break_aborts_without_yes(in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `-y` and without input piped in, the confirmation prompt aborts."""
    from typer.testing import CliRunner

    from tf_project.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["self", "lock", "break"], input="n\n")
    assert result.exit_code != 0
