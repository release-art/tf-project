"""Exhaustive `tfp <command>` dispatch tests.

Each test runs through `CliRunner.invoke` and asserts that the typer
handler dispatches to the right `commands.do_*` or `self_commands.do_*`
function with the right arguments. The underlying functions are
monkey-patched to no-ops/spies so these tests stay fast (no terraform).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from typer.testing import CliRunner

from tf_project.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def in_project(project_tree: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:  # type: ignore[no-untyped-def]
    """Place us inside a configured project so every command can load Config."""
    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    return project_tree


# ---- Helpers ----------------------------------------------------------------


def _spy(monkeypatch: pytest.MonkeyPatch, target: str) -> list[dict[str, object]]:
    """Replace `target` with a spy that records every call's kwargs+args."""
    calls: list[dict[str, object]] = []

    def spy(*args: object, **kwargs: object) -> object:
        calls.append({"args": args, "kwargs": kwargs})
        return None

    monkeypatch.setattr(target, spy)
    return calls


# ---- Top-level wrapped commands ---------------------------------------------


def test_plan_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_plan")
    result = runner.invoke(app, ["plan", "-t", "a.b", "-r", "c.d"])
    assert result.exit_code == 0, result.output
    assert calls and calls[0]["kwargs"]["targets"] == ["a.b"]
    assert calls[0]["kwargs"]["replaces"] == ["c.d"]


def test_apply_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_apply")
    result = runner.invoke(app, ["apply", "--force"])
    assert result.exit_code == 0
    assert calls and calls[0]["kwargs"]["force"] is True


def test_refresh_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_refresh")
    result = runner.invoke(app, ["refresh", "-t", "x.y"])
    assert result.exit_code == 0
    assert calls and calls[0]["kwargs"]["targets"] == ["x.y"]


def test_destroy_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_destroy")
    result = runner.invoke(app, ["destroy", "-t", "x.y"])
    assert result.exit_code == 0
    assert calls and calls[0]["kwargs"]["targets"] == ["x.y"]


def test_fmt_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_fmt")
    result = runner.invoke(app, ["fmt"])
    assert result.exit_code == 0
    assert calls


def test_output_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_output")
    result = runner.invoke(app, ["output"])
    assert result.exit_code == 0
    assert calls


def test_import_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_import")
    result = runner.invoke(app, ["import", "aws_s3_bucket.foo", "my-bucket"])
    assert result.exit_code == 0
    assert calls
    kw = calls[0]["kwargs"]
    assert kw["address"] == "aws_s3_bucket.foo"
    assert kw["resource_id"] == "my-bucket"


# ---- state subcommand group --------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_sub", "expected_args"),
    [
        (["state", "list"], "list", []),
        (["state", "list", "aws_s3.foo"], "list", ["aws_s3.foo"]),
        (["state", "show", "aws_s3.foo"], "show", ["aws_s3.foo"]),
        (["state", "mv", "a", "b"], "mv", ["a", "b"]),
        (["state", "rm", "a"], "rm", ["a"]),
        (["state", "pull"], "pull", []),
        (["state", "replace-provider", "x", "y"], "replace-provider", ["x", "y"]),
        (["state", "identities"], "identities", []),
    ],
)
def test_state_subcommands_dispatch(
    runner: CliRunner,
    in_project: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_sub: str,
    expected_args: list[str],
) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_state")
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert calls and calls[0]["kwargs"]["subcommand"] == expected_sub
    assert calls[0]["kwargs"]["args"] == expected_args


def test_state_push_dispatches(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    state_file = tmp_path / "backup.tfstate"
    state_file.write_text("{}")
    calls = _spy(monkeypatch, "tf_project.cli.commands.do_state")
    result = runner.invoke(app, ["state", "push", str(state_file)])
    assert result.exit_code == 0
    assert calls and calls[0]["kwargs"]["subcommand"] == "push"
    assert calls[0]["kwargs"]["args"] == [str(state_file)]


# ---- use / ls / rm ----------------------------------------------------------


def test_use_surfaces_slot_conflict_error(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project import slots as slots_mod

    def boom(*a: object, **kw: object) -> None:
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
        b_slot = a_slot.model_copy(update={"name_is_explicit": False, "tfvars": pathlib.Path("/b")})
        raise slots_mod.SlotConflictError(a_slot, b_slot)

    monkeypatch.setattr("tf_project.cli.commands.do_use", boom)
    result = runner.invoke(app, ["use", "x"])
    assert result.exit_code == 1
    assert "defined by 2 tfvars files" in result.output or "defined by 2 tfvars files" in (result.stderr or "")


def test_use_surfaces_slot_error(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project import slots as slots_mod

    def boom(*a: object, **kw: object) -> None:
        raise slots_mod.SlotError("no tfvars defines slot 'ghost'")

    monkeypatch.setattr("tf_project.cli.commands.do_use", boom)
    result = runner.invoke(app, ["use", "ghost"])
    assert result.exit_code == 1


def test_ls_empty(runner: CliRunner, tmp_path: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    # Project with no tfvars at all.
    (tmp_path / "terraform").mkdir()
    (tmp_path / "tfvars").mkdir()
    (tmp_path / "tmp").mkdir()
    write_tfp_toml(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "no slots found" in result.output


def test_ls_json(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["ls", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "slots" in payload and "orphans" in payload


def test_ls_orphan_section(
    runner: CliRunner, in_project: pathlib.Path
) -> None:
    # Create a slot dir with no matching banner.
    (in_project / "tmp" / "slots" / "ghost").mkdir(parents=True)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "orphans" in result.output and "ghost" in result.output


def test_rm_surfaces_not_found(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project import slots as slots_mod

    def boom(*a: object, **kw: object) -> None:
        raise slots_mod.SlotNotFoundError("no slot directory at /ghost")

    monkeypatch.setattr("tf_project.cli.commands.do_rm", boom)
    result = runner.invoke(app, ["rm", "ghost"])
    assert result.exit_code == 1


def test_rm_dispatches(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tf_project.cli.commands.do_rm", lambda *a, **kw: pathlib.Path("/x"))
    result = runner.invoke(app, ["rm", "dev", "--force"])
    assert result.exit_code == 0
    assert "removed" in result.output


# ---- last / status ----------------------------------------------------------


def test_last_dispatches(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_last_invocation",
        lambda config, slot=None: {
            "timestamp": "2026-05-22T12:00:00+00:00",
            "argv": ["terraform", "plan"],
            "exit_code": 0,
        },
    )
    result = runner.invoke(app, ["last"])
    assert result.exit_code == 0
    assert "terraform plan" in result.output


def test_last_no_record(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.self_commands import SelfCommandError

    def boom(*a: object, **kw: object) -> dict[str, object]:
        raise SelfCommandError("No prior invocation")

    monkeypatch.setattr("tf_project.cli.self_commands.do_last_invocation", boom)
    result = runner.invoke(app, ["last"])
    assert result.exit_code == 1


def test_status_uninitialized(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Not initialized" in result.output


def test_status_initialized(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "slot" in result.output and "dev" in result.output


# ---- self subcommands -------------------------------------------------------


def test_self_init_dispatches(runner: CliRunner, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["self", "init"])
    assert result.exit_code == 0
    assert (tmp_path / "tf_project.toml").exists()


def test_self_init_refuses_to_overwrite(
    runner: CliRunner, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tf_project.toml").write_text("[tf_project]\n")
    result = runner.invoke(app, ["self", "init"])
    assert result.exit_code == 1


def test_self_config_print(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["self", "config", "print"])
    assert result.exit_code == 0
    assert "terraform_dir" in result.output


def test_self_config_print_json(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["self", "config", "print", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "terraform_dir" in payload


def test_self_config_path(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["self", "config", "path"])
    assert result.exit_code == 0
    assert "tf_project.toml" in result.output


def test_self_state_show_no_active(runner: CliRunner, in_project: pathlib.Path) -> None:
    result = runner.invoke(app, ["self", "state", "show"])
    assert result.exit_code == 1


def test_self_state_show_dispatches(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    result = runner.invoke(app, ["self", "state", "show"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["slot"] == "dev"


def test_self_state_clear_when_absent(runner: CliRunner, in_project: pathlib.Path) -> None:
    # No active slot — passing --slot explicitly via -s
    result = runner.invoke(app, ["-s", "dev", "self", "state", "clear"])
    assert result.exit_code == 0
    assert "No state file" in result.output


def test_self_state_clear_dispatches(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    result = runner.invoke(app, ["self", "state", "clear"])
    assert result.exit_code == 0
    assert "Removed" in result.output


def test_self_doctor_ok(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force a clean doctor outcome.
    from tf_project.self_commands import DoctorCheck

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_doctor",
        lambda config: [DoctorCheck(name="x", ok=True, detail="d")],
    )
    result = runner.invoke(app, ["self", "doctor"])
    assert result.exit_code == 0


def test_self_doctor_failing(runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tf_project.self_commands import DoctorCheck

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_doctor",
        lambda config: [DoctorCheck(name="x", ok=False, detail="d")],
    )
    result = runner.invoke(app, ["self", "doctor"])
    assert result.exit_code == 1


def test_self_trace_dispatches(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    result = runner.invoke(app, ["self", "trace", "plan"])
    assert result.exit_code == 0
    assert "plan" in result.output


def test_self_trace_unknown(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    result = runner.invoke(app, ["self", "trace", "bogus"])
    assert result.exit_code == 1


def test_self_banner_check(
    runner: CliRunner, in_project: pathlib.Path, tfvars: pathlib.Path
) -> None:
    result = runner.invoke(app, ["self", "banner", "check", str(tfvars)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["project"] == "demo"


def test_self_snapshot_dispatches(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    written = tmp_path / "snapshot.tfstate"
    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_snapshot",
        lambda config, slot=None, dest=None: written,
    )
    result = runner.invoke(app, ["self", "snapshot"])
    assert result.exit_code == 0
    assert "snapshot" in result.output


def test_self_snapshot_failure(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import SelfCommandError

    def boom(*a: object, **kw: object) -> pathlib.Path:
        raise SelfCommandError("nope")

    monkeypatch.setattr("tf_project.cli.self_commands.do_self_snapshot", boom)
    result = runner.invoke(app, ["self", "snapshot"])
    assert result.exit_code == 1


def test_self_providers_lock_dispatches(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch, "tf_project.cli.self_commands.do_self_providers_lock")
    result = runner.invoke(app, ["self", "providers", "lock", "-p", "linux_amd64"])
    assert result.exit_code == 0
    assert calls and calls[0]["kwargs"]["platforms"] == ["linux_amd64"]


def test_self_providers_lock_failure(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import SelfCommandError

    def boom(*a: object, **kw: object) -> None:
        raise SelfCommandError("nope")

    monkeypatch.setattr("tf_project.cli.self_commands.do_self_providers_lock", boom)
    result = runner.invoke(app, ["self", "providers", "lock"])
    assert result.exit_code == 1


def test_self_lock_status_locked(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import LockStatus

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_lock_status",
        lambda config, slot=None: LockStatus(
            backend="azurerm",
            locked=True,
            detail="lease_state=leased",
            lock_id="abc",
            lock_who="me",
            lock_operation="OperationTypePlan",
            lock_created="2026",
        ),
    )
    result = runner.invoke(app, ["self", "lock", "status"])
    assert result.exit_code == 2  # locked → exit 2
    assert "abc" in result.output


def test_self_lock_status_unlocked(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import LockStatus

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_lock_status",
        lambda config, slot=None: LockStatus(backend="azurerm", locked=False, detail="ok"),
    )
    result = runner.invoke(app, ["self", "lock", "status"])
    assert result.exit_code == 0


def test_self_lock_status_failure(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import SelfCommandError

    def boom(*a: object, **kw: object) -> object:
        raise SelfCommandError("nope")

    monkeypatch.setattr("tf_project.cli.self_commands.do_self_lock_status", boom)
    result = runner.invoke(app, ["self", "lock", "status"])
    assert result.exit_code == 1


def test_self_lock_break_polite(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import BreakResult

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_lock_break",
        lambda config, slot=None, blunt=False: BreakResult(
            method="force-unlock", backend="azurerm", lock_id="abc"
        ),
    )
    result = runner.invoke(app, ["self", "lock", "break", "-y"])
    assert result.exit_code == 0
    assert "force-unlock" in result.output


def test_self_lock_break_blunt(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import BreakResult

    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_lock_break",
        lambda config, slot=None, blunt=False: BreakResult(method="lease-break", backend="s3"),
    )
    result = runner.invoke(app, ["self", "lock", "break", "-y", "--blunt"])
    assert result.exit_code == 0
    assert "lease" in result.output or "s3" in result.output


def test_self_lock_break_failure(
    runner: CliRunner, in_project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.self_commands import SelfCommandError

    def boom(*a: object, **kw: object) -> object:
        raise SelfCommandError("nope")

    monkeypatch.setattr("tf_project.cli.self_commands.do_self_lock_break", boom)
    result = runner.invoke(app, ["self", "lock", "break", "-y"])
    assert result.exit_code == 1
