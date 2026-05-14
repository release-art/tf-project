"""Tests for terraform.run / exec_passthrough runtime behavior."""

from __future__ import annotations

import subprocess

import pytest

from tf_project import terraform


@pytest.fixture(autouse=True)
def _reset_runtime_options() -> None:
    terraform.set_runtime_options(dry_run=False, verbose=False)


def test_run_clean_exit_on_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        def wait(self) -> int:
            return 0

    monkeypatch.setattr(terraform.subprocess, "Popen", lambda *a, **kw: FakeProc())
    terraform.run(["terraform", "version"])


def test_run_raises_terraform_exit_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProc:
        def wait(self) -> int:
            return 2

    monkeypatch.setattr(terraform.subprocess, "Popen", lambda *a, **kw: FakeProc())
    with pytest.raises(terraform.TerraformExit) as excinfo:
        terraform.run(["terraform", "plan"])
    assert excinfo.value.code == 2


def test_run_exits_127_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(terraform.subprocess, "Popen", boom)
    with pytest.raises(terraform.TerraformExit) as excinfo:
        terraform.run(["nope"])
    assert excinfo.value.code == 127


def test_run_keyboard_interrupt_then_clean_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """KeyboardInterrupt during wait() should be swallowed once."""

    class FakeProc:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self) -> int:
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt
            return 130

    proc = FakeProc()
    monkeypatch.setattr(terraform.subprocess, "Popen", lambda *a, **kw: proc)
    with pytest.raises(terraform.TerraformExit) as excinfo:
        terraform.run(["terraform", "apply"])
    assert excinfo.value.code == 130
    assert proc.calls == 2


def test_dry_run_skips_execution(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise AssertionError("subprocess should not be invoked in dry-run mode")

    monkeypatch.setattr(terraform.subprocess, "Popen", boom)
    terraform.set_runtime_options(dry_run=True)
    terraform.run(["terraform", "plan", "-out=foo"])
    err = capsys.readouterr().err
    assert "[dry-run]" in err
    assert "plan" in err


def test_verbose_logs_argv(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeProc:
        def wait(self) -> int:
            return 0

    monkeypatch.setattr(terraform.subprocess, "Popen", lambda *a, **kw: FakeProc())
    terraform.set_runtime_options(verbose=True)
    terraform.run(["terraform", "validate"])
    err = capsys.readouterr().err
    assert "$ terraform validate" in err


def test_exec_passthrough_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execvp(name: str, argv: list[str]) -> None:
        captured["execvp"] = (name, argv)

    monkeypatch.setattr(terraform.os, "execvp", fake_execvp)
    terraform.exec_passthrough(["terraform", "version"])
    assert captured["execvp"] == ("terraform", ["terraform", "version"])


def test_exec_passthrough_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_execvpe(name: str, argv: list[str], env: dict[str, str]) -> None:
        captured["execvpe"] = (name, argv, env)

    monkeypatch.setattr(terraform.os, "execvpe", fake_execvpe)
    terraform.exec_passthrough(["terraform", "version"], env={"X": "1"})
    assert captured["execvpe"] == ("terraform", ["terraform", "version"], {"X": "1"})


def test_exec_passthrough_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **kw: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(terraform.os, "execvp", boom)
    with pytest.raises(terraform.TerraformExit) as excinfo:
        terraform.exec_passthrough(["nope"])
    assert excinfo.value.code == 127


# Sanity: subprocess is imported as a module from terraform
def test_subprocess_module_used() -> None:
    assert hasattr(terraform.subprocess, "Popen")
    _ = subprocess  # keep the import live for IDE / lint clarity
