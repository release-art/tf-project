from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from tf_project import commands
from tf_project.config import Config
from tf_project.state import MyState


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        calls.append({"cmd": cmd, "env": env})

    monkeypatch.setattr("tf_project.commands.terraform.run", fake_run)
    monkeypatch.setattr("tf_project.commands.terraform.exec_passthrough", fake_run)
    return calls


def _save_state(config: Config, *, tfvars: pathlib.Path) -> MyState:
    state = MyState(
        tfvars=str(tfvars),
        source_root=str(config.terraform_dir / "demo"),
        tfplan_location=str(config.tfplan_file),
        environ={"ARM_SUBSCRIPTION_ID": "abc"},
        backend_config={"key": "terraform/azure/dev.tfstate"},
    )
    state.save(config)
    return state


def test_init_builds_command_and_persists_state(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    commands.do_init(config, tfvars=tfvars)
    assert len(run_calls) == 1
    cmd = run_calls[0]["cmd"]
    assert cmd[0] == config.terraform_binary
    assert f"-chdir={config.terraform_dir / 'demo'}" in cmd
    assert "init" in cmd
    assert "-upgrade" in cmd
    assert "-reconfigure" in cmd
    assert "-backend-config" in cmd
    idx = cmd.index("-backend-config")
    assert cmd[idx + 1] == "key=terraform/azure/dev.tfstate"
    assert config.state_file.exists()


def test_init_preserves_prior_environ(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_init(config, tfvars=tfvars)
    new_state = MyState.load(config)
    assert new_state is not None
    assert new_state.environ == {"ARM_SUBSCRIPTION_ID": "abc"}


def test_plan_targets_and_replaces(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_plan(config, targets=["a.b", "c.d"], replaces=["e.f"])
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert "plan" in cmd
    assert "-target=a.b" in cmd
    assert "-target=c.d" in cmd
    assert "-replace=e.f" in cmd
    assert any(arg.startswith("-out=") and arg.endswith("my.tfplan") for arg in cmd)
    assert any(arg.startswith("-var-file=") for arg in cmd)
    assert isinstance(env, dict)
    assert env["ARM_SUBSCRIPTION_ID"] == "abc"


def test_apply_uses_saved_plan_and_unlinks(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_state(config, tfvars=tfvars)
    config.tfplan_file.write_bytes(b"plan")
    commands.do_apply(config)
    cmd = run_calls[0]["cmd"]
    assert "apply" in cmd
    assert str(config.tfplan_file) in cmd
    assert not config.tfplan_file.exists()


def test_destroy_targets(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_destroy(config, targets=["x.y"])
    cmd = run_calls[0]["cmd"]
    assert "destroy" in cmd
    assert "-target=x.y" in cmd


def test_fmt_formats_existing_dirs(config: Config, run_calls: list[dict[str, object]]) -> None:
    commands.do_fmt(config)
    cmd = run_calls[0]["cmd"]
    assert cmd[:3] == ["terraform", "fmt", "-recursive"]
    assert str(config.terraform_dir) in cmd
    assert str(config.tfvars_dir) in cmd


def test_output_requires_state(config: Config) -> None:
    with pytest.raises(commands.StateNotInitializedError):
        commands.do_output(config)


def test_state_mv(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_state_mv(config, source="a.b", destination="c.d")
    cmd = run_calls[0]["cmd"]
    assert cmd[-4:] == ["state", "mv", "a.b", "c.d"]


def test_plan_extra_args_forwarded(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_plan(config, extra=["-detailed-exitcode", "-compact-warnings"])
    cmd = run_calls[0]["cmd"]
    assert "-detailed-exitcode" in cmd
    assert "-compact-warnings" in cmd
    # extras should land before -out= so terraform parses them as plan flags
    assert cmd.index("-detailed-exitcode") < next(i for i, a in enumerate(cmd) if a.startswith("-out="))


def test_passthrough_with_state(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_passthrough(config, ["validate", "-json"])
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert cmd[0] == "terraform"
    assert f"-chdir={config.terraform_dir / 'demo'}" in cmd
    assert cmd[-2:] == ["validate", "-json"]
    assert isinstance(env, dict) and env["ARM_SUBSCRIPTION_ID"] == "abc"


def test_passthrough_without_state(config: Config, run_calls: list[dict[str, object]]) -> None:
    commands.do_passthrough(config, ["version"])
    cmd = run_calls[0]["cmd"]
    assert cmd == ["terraform", "version"]
    assert run_calls[0]["env"] is None


def test_custom_terraform_binary_used(config: Config, run_calls: list[dict[str, object]]) -> None:
    cfg = dataclasses.replace(config, terraform_binary="/opt/tofu/bin/tofu")
    commands.do_fmt(cfg)
    assert run_calls[0]["cmd"][0] == "/opt/tofu/bin/tofu"


def _write_banner_tfvars(path: pathlib.Path, banner: dict[str, object]) -> None:
    path.write_text(f'# {json.dumps(banner)}\nfoo = "bar"\n')


def test_init_honours_banner_state_key(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "state_key": "shared/global.tfstate"},
    )
    commands.do_init(config, tfvars=tfvars)
    state = MyState.load(config)
    assert state is not None
    assert state.backend_config == {"key": "shared/global.tfstate"}
    cmd = run_calls[0]["cmd"]
    idx = cmd.index("-backend-config")
    assert cmd[idx + 1] == "key=shared/global.tfstate"


def test_init_honours_banner_env(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _write_banner_tfvars(
        tfvars,
        {
            "header": "terraform",
            "project": "demo",
            "env": {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"},
        },
    )
    commands.do_init(config, tfvars=tfvars)
    state = MyState.load(config)
    assert state is not None
    assert state.environ == {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"}


def test_banner_env_overrides_prior_state_env(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_state(config, tfvars=tfvars)  # seeds environ={"ARM_SUBSCRIPTION_ID": "abc"}
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"ARM_SUBSCRIPTION_ID": "new"}},
    )
    commands.do_init(config, tfvars=tfvars)
    state = MyState.load(config)
    assert state is not None
    assert state.environ["ARM_SUBSCRIPTION_ID"] == "new"


def test_banner_env_extends_prior_state_env(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_state(config, tfvars=tfvars)  # has ARM_SUBSCRIPTION_ID=abc
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"NEW_VAR": "y"}},
    )
    commands.do_init(config, tfvars=tfvars)
    state = MyState.load(config)
    assert state is not None
    assert state.environ == {"ARM_SUBSCRIPTION_ID": "abc", "NEW_VAR": "y"}


def test_banner_invalid_state_key_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(tfvars, {"header": "terraform", "project": "demo", "state_key": ""})
    with pytest.raises(ValueError, match="state_key"):
        commands.do_init(config, tfvars=tfvars)


def test_banner_invalid_env_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"OK": 123}},
    )
    with pytest.raises(ValueError, match="env"):
        commands.do_init(config, tfvars=tfvars)


def test_plan_writes_tfplan_meta(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_plan(config)
    meta = pathlib.Path(config.tfplan_file.as_posix() + commands.TFPLAN_META_SUFFIX)
    assert meta.exists()
    payload = json.loads(meta.read_text())
    assert "tfvars_sha256" in payload


def test_apply_rejects_stale_tfplan(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    config.tfplan_file.write_bytes(b"plan")
    # Write a meta file claiming the tfplan was generated against different content.
    meta = pathlib.Path(config.tfplan_file.as_posix() + commands.TFPLAN_META_SUFFIX)
    meta.write_text(json.dumps({"tfvars_sha256": "deadbeef" * 8}))
    with pytest.raises(commands.StaleTfplanError):
        commands.do_apply(config)
    # File still present — not consumed.
    assert config.tfplan_file.exists()


def test_apply_force_bypasses_stale_guard(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_state(config, tfvars=tfvars)
    config.tfplan_file.write_bytes(b"plan")
    meta = pathlib.Path(config.tfplan_file.as_posix() + commands.TFPLAN_META_SUFFIX)
    meta.write_text(json.dumps({"tfvars_sha256": "wrong"}))
    commands.do_apply(config, force=True)
    assert not config.tfplan_file.exists()
    assert not meta.exists()


def test_apply_consumes_fresh_tfplan_meta(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_state(config, tfvars=tfvars)
    commands.do_plan(config)  # writes meta
    config.tfplan_file.write_bytes(b"plan")  # simulate terraform writing the tfplan
    commands.do_apply(config)  # should not raise — meta matches current tfvars
    assert not config.tfplan_file.exists()
    assert not pathlib.Path(config.tfplan_file.as_posix() + commands.TFPLAN_META_SUFFIX).exists()


def test_status_report_uninitialized(config: Config) -> None:
    report = commands.status_report(config)
    assert report.initialized is False


def test_status_report_after_init(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_state(config, tfvars=tfvars)
    report = commands.status_report(config)
    assert report.initialized is True
    assert report.tfvars == str(tfvars)
    assert report.backend_key == "terraform/azure/dev.tfstate"
    assert "ARM_SUBSCRIPTION_ID" in report.env_keys
    assert report.plan_ready is False
    config.tfplan_file.write_bytes(b"plan")
    assert commands.status_report(config).plan_ready is True


def test_init_passes_backend_config_table(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    cfg = dataclasses.replace(
        config,
        backend_config={"resource_group_name": "rg", "storage_account_name": "sa"},
    )
    commands.do_init(cfg, tfvars=tfvars)
    cmd = run_calls[0]["cmd"]
    assert isinstance(cmd, list)
    flat = " ".join(cmd)
    assert "-backend-config resource_group_name=rg" in flat
    assert "-backend-config storage_account_name=sa" in flat


def test_passthrough_uses_exec(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    """do_passthrough must invoke terraform.exec_passthrough, not terraform.run."""
    exec_calls: list[list[str]] = []
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.exec_passthrough",
        lambda cmd, env=None: exec_calls.append(cmd),
    )
    monkeypatch.setattr(
        "tf_project.commands.terraform.run",
        lambda cmd, env=None: run_calls.append(cmd),
    )
    commands.do_passthrough(config, ["version"])
    assert exec_calls and exec_calls[0] == [config.terraform_binary, "version"]
    assert run_calls == []
