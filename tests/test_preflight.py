from __future__ import annotations

import pathlib
from collections.abc import Callable

import pytest

from tf_project import commands, preflight
from tf_project.config import Config
from tf_project.models import MyState


def test_check_data_dir_passes_when_present(config: Config) -> None:
    config.data_dir_for("dev").mkdir(parents=True)
    preflight.check_data_dir(config, "dev")


def test_check_data_dir_raises_when_missing(config: Config) -> None:
    with pytest.raises(preflight.RecoveryError) as exc:
        preflight.check_data_dir(config, "dev")
    assert "tfp use dev" in str(exc.value)
    assert ".terraform" in str(exc.value)


def test_check_apply_plan_both_present(config: Config) -> None:
    slot_dir = config.slot_dir_for("dev")
    slot_dir.mkdir(parents=True)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    config.tfplan_meta_for("dev").write_text("{}")
    preflight.check_apply_plan(config, "dev")


def test_check_apply_plan_both_missing(config: Config) -> None:
    with pytest.raises(preflight.RecoveryError) as exc:
        preflight.check_apply_plan(config, "dev")
    assert "tfp plan" in str(exc.value)


def test_check_apply_plan_half_deleted_plan_present(config: Config) -> None:
    slot_dir = config.slot_dir_for("dev")
    slot_dir.mkdir(parents=True)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    # meta missing
    with pytest.raises(preflight.RecoveryError) as exc:
        preflight.check_apply_plan(config, "dev")
    assert "half-deleted" in str(exc.value)
    assert "tfplan.meta.json" in str(exc.value)


def test_check_apply_plan_half_deleted_meta_present(config: Config) -> None:
    slot_dir = config.slot_dir_for("dev")
    slot_dir.mkdir(parents=True)
    config.tfplan_meta_for("dev").write_text("{}")
    # plan missing
    with pytest.raises(preflight.RecoveryError) as exc:
        preflight.check_apply_plan(config, "dev")
    assert "half-deleted" in str(exc.value)


# ---- Integration: wrapped commands surface RecoveryError -----------------------


def test_do_plan_recovery_when_data_dir_missing(
    config: Config, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    # Simulate user `rm -rf`'ing the data dir.
    import shutil

    shutil.rmtree(config.data_dir_for("dev"))
    with pytest.raises(preflight.RecoveryError) as exc:
        commands.do_plan(config)
    assert "tfp use dev" in str(exc.value)
    assert run_calls == []


def test_do_apply_recovery_when_plan_meta_half_deleted(
    config: Config, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    config.tfplan_file_for("dev").write_bytes(b"plan")
    # meta missing - half-state from a hand-edit.
    with pytest.raises(preflight.RecoveryError) as exc:
        commands.do_apply(config)
    assert "half-deleted" in str(exc.value)
    assert run_calls == []


def test_do_passthrough_recovery_when_data_dir_missing(
    config: Config, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    import shutil

    shutil.rmtree(config.data_dir_for("dev"))
    with pytest.raises(preflight.RecoveryError):
        commands.do_passthrough(config, ["validate"])
    assert run_calls == []


def test_do_use_runs_providers_lock_after_init(
    config: Config, run_calls: list[dict[str, object]]
) -> None:
    commands.do_use(config, arg="dev")
    cmds = [call["cmd"] for call in run_calls]
    assert len(cmds) == 2, cmds
    assert "init" in cmds[0]
    assert cmds[1][2:4] == ["providers", "lock"]
    assert "-platform=linux_amd64" in cmds[1]
    assert "-platform=linux_arm64" in cmds[1]
    assert f"-platform={commands.current_terraform_platform()}" in cmds[1]


def test_do_use_includes_extra_lock_platforms(
    config: Config, run_calls: list[dict[str, object]]
) -> None:
    cfg = config.model_copy(update={"extra_lock_platforms": ("windows_amd64", "freebsd_amd64")})
    commands.do_use(cfg, arg="dev")
    lock_cmd = run_calls[1]["cmd"]
    assert "-platform=windows_amd64" in lock_cmd
    assert "-platform=freebsd_amd64" in lock_cmd
    # Base + host still present.
    assert "-platform=linux_amd64" in lock_cmd
    assert "-platform=linux_arm64" in lock_cmd
    assert f"-platform={commands.current_terraform_platform()}" in lock_cmd
