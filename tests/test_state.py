from __future__ import annotations

import pathlib

import pytest

from tf_project.config import Config
from tf_project.secrets import CommandProvider, NoopProvider
from tf_project.state import MyState


def _make_state(tfvars: pathlib.Path) -> MyState:
    return MyState(
        tfvars=str(tfvars),
        source_root="/tmp/src",
        tfplan_location="/tmp/my.tfplan",
        environ={"X": "1"},
        backend_config={"key": "k"},
    )


def test_round_trip(config: Config, tfvars: pathlib.Path) -> None:
    state = _make_state(tfvars)
    state.save(config)
    loaded = MyState.load(config)
    assert loaded is not None
    assert loaded.tfvars == state.tfvars
    assert loaded.environ == {"X": "1"}
    assert loaded.backend_config == {"key": "k"}


def test_load_returns_none_when_absent(config: Config) -> None:
    assert MyState.load(config) is None


def test_decrypted_tfvars_noop(config: Config, tfvars: pathlib.Path) -> None:
    state = _make_state(tfvars)
    with state.decrypted_tfvars(NoopProvider()) as p:
        assert p == tfvars


def test_decrypted_tfvars_cleanup_on_exception(
    config: Config,
    tfvars: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(tfvars)

    captured: dict[str, pathlib.Path] = {}

    def fake_check_call(cmd: list[str]) -> int:
        # Simulate `op inject` writing to the rendered out path.
        out_path = pathlib.Path(cmd[-1])
        out_path.write_text("decrypted")
        captured["out"] = out_path
        return 0

    monkeypatch.setattr("tf_project.secrets.subprocess.check_call", fake_check_call)
    provider = CommandProvider(("echo", "{in}", "{out}"))
    with pytest.raises(RuntimeError, match="boom"):
        with state.decrypted_tfvars(provider):
            raise RuntimeError("boom")
    assert "out" in captured
    assert not captured["out"].exists()
