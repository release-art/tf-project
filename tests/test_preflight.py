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


def test_dep_lock_warning_lock_present(tmp_path: pathlib.Path) -> None:
    (tmp_path / ".terraform.lock.hcl").write_text("# lock\n")
    assert preflight.dep_lock_warning(tmp_path, plugin_cache_enabled=True) is None


def test_dep_lock_warning_missing_but_cache_disabled(tmp_path: pathlib.Path) -> None:
    assert preflight.dep_lock_warning(tmp_path, plugin_cache_enabled=False) is None


def test_dep_lock_warning_missing_with_cache(tmp_path: pathlib.Path) -> None:
    msg = preflight.dep_lock_warning(tmp_path, plugin_cache_enabled=True)
    assert msg is not None
    assert "tfp self providers lock" in msg


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
    # meta missing — half-state from a hand-edit.
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


def test_do_use_warns_when_dep_lock_missing(
    config: Config, capsys: pytest.CaptureFixture[str], run_calls: list[dict[str, object]]
) -> None:
    # Source root exists from project_tree fixture but has no .terraform.lock.hcl.
    commands.do_use(config, arg="dev")
    err = capsys.readouterr().err
    assert ".terraform.lock.hcl is missing" in err
    assert "tfp self providers lock" in err


def test_do_use_no_warning_when_dep_lock_present(
    config: Config, capsys: pytest.CaptureFixture[str], run_calls: list[dict[str, object]]
) -> None:
    source_root = (config.terraform_dir / "demo").resolve()
    (source_root / ".terraform.lock.hcl").write_text("# lock\n")
    commands.do_use(config, arg="dev")
    err = capsys.readouterr().err
    assert ".terraform.lock.hcl is missing" not in err
