from __future__ import annotations

import pathlib

import pytest

from tf_project.config import Config
from tf_project.models import MyState
from tf_project.secrets import CommandProvider, NoopProvider
from tf_project.state import (
    load_state,
    read_active,
    save_state,
    write_active,
)


def _make_state(tfvars: pathlib.Path) -> MyState:
    return MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=pathlib.Path("/tmp/src"),
        tfplan_location=pathlib.Path("/tmp/my.tfplan"),
        environ={"X": "1"},
        backend_config={"key": "k"},
    )


def test_round_trip(config: Config, tfvars: pathlib.Path) -> None:
    state = _make_state(tfvars)
    save_state(state, config)
    loaded = load_state(config, "dev")
    assert loaded is not None
    assert loaded.tfvars == state.tfvars
    assert loaded.environ == {"X": "1"}
    assert loaded.backend_config == {"key": "k"}
    assert loaded.slot == "dev"


def test_load_returns_none_when_absent(config: Config) -> None:
    assert load_state(config, "dev") is None


def test_decrypted_tfvars_noop(config: Config, tfvars: pathlib.Path) -> None:
    state = _make_state(tfvars)
    with NoopProvider().materialize(state.tfvars) as p:
        assert p == tfvars


def test_decrypted_tfvars_shredded_on_normal_exit(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state(tfvars)
    captured: dict[str, pathlib.Path] = {}

    def fake_check_call(cmd: list[str]) -> int:
        out_path = pathlib.Path(cmd[-1])
        out_path.write_text("decrypted secret payload")
        captured["out"] = out_path
        return 0

    monkeypatch.setattr("tf_project.secrets.subprocess.check_call", fake_check_call)
    provider = CommandProvider(("echo", "{in}", "{out}"))
    with provider.materialize(state.tfvars) as path:
        assert path.exists()
        assert path.read_text() == "decrypted secret payload"
    assert "out" in captured
    assert not captured["out"].exists()


def test_save_creates_lock_file(config: Config, tfvars: pathlib.Path) -> None:
    state = _make_state(tfvars)
    save_state(state, config)
    state_path = config.state_file_for("dev")
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    assert lock_path.exists()


def test_decrypted_tfvars_cleanup_on_exception(
    config: Config,
    tfvars: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tfvars)
    captured: dict[str, pathlib.Path] = {}

    def fake_check_call(cmd: list[str]) -> int:
        out_path = pathlib.Path(cmd[-1])
        out_path.write_text("decrypted")
        captured["out"] = out_path
        return 0

    monkeypatch.setattr("tf_project.secrets.subprocess.check_call", fake_check_call)
    provider = CommandProvider(("echo", "{in}", "{out}"))
    with pytest.raises(RuntimeError, match="boom"):
        with provider.materialize(state.tfvars):
            raise RuntimeError("boom")
    assert "out" in captured
    assert not captured["out"].exists()


def test_active_pointer_round_trip(config: Config) -> None:
    assert read_active(config) is None
    write_active(config, "dev")
    assert read_active(config) == "dev"
    write_active(config, "prod")
    assert read_active(config) == "prod"
