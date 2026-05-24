from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import pytest

from tf_project import commands, slots
from tf_project.config import Config
from tf_project.models import MyState
from tf_project.state import load_state


def test_do_use_runs_init_and_persists_state(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    commands.do_use(config, arg="dev")
    # init + post-init `providers lock`
    assert len(run_calls) == 2
    cmd = run_calls[0]["cmd"]
    assert cmd[0] == config.terraform_binary
    assert f"-chdir={config.terraform_dir / 'demo'}" in cmd
    assert "init" in cmd
    assert "-upgrade" in cmd
    assert "-reconfigure" in cmd
    assert "-backend-config" in cmd
    idx = cmd.index("-backend-config")
    assert cmd[idx + 1] == "key=terraform/azure/dev.tfstate"
    lock_cmd = run_calls[1]["cmd"]
    assert lock_cmd[2:4] == ["providers", "lock"]
    assert config.state_file_for("dev").exists()
    assert config.active_file.read_text().strip() == "dev"


def test_do_use_warm_skips_init(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.do_use(config, arg="dev")
    assert len(run_calls) == 2  # init + providers lock
    # Simulate terraform having written its data-dir marker so the warmth
    # check believes init has run.
    (config.data_dir_for("dev")).mkdir(parents=True, exist_ok=True)
    (config.data_dir_for("dev") / "terraform.tfstate").write_text("{}")
    run_calls.clear()
    commands.do_use(config, arg="dev")
    assert run_calls == []


def test_do_use_by_path(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.do_use(config, arg=str(tfvars))
    assert config.state_file_for("dev").exists()


def test_do_use_init_failure_wipes_slot_dir(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project import terraform as terraform_mod

    def fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        raise terraform_mod.TerraformExit(2)

    monkeypatch.setattr("tf_project.commands.terraform.run", fake_run)
    with pytest.raises(terraform_mod.TerraformExit):
        commands.do_use(config, arg="dev")
    assert not config.slot_dir_for("dev").exists()


def test_do_use_reinit_failure_preserves_prior_state(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-init failure must not wipe a previously-good state.json."""
    from tf_project import terraform as terraform_mod

    commands.do_use(config, arg="dev")
    state_before = config.state_file_for("dev").read_text()
    # Force re-init by deleting backend.hash so _decide_init returns True.
    config.backend_hash_for("dev").unlink()

    def boom(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        raise terraform_mod.TerraformExit(2)

    monkeypatch.setattr("tf_project.commands.terraform.run", boom)
    with pytest.raises(terraform_mod.TerraformExit):
        commands.do_use(config, arg="dev")

    assert config.slot_dir_for("dev").exists()
    assert config.state_file_for("dev").read_text() == state_before


def test_plan_targets_and_replaces(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_plan(config, targets=["a.b", "c.d"], replaces=["e.f"])
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert "plan" in cmd
    assert "-target=a.b" in cmd
    assert "-target=c.d" in cmd
    assert "-replace=e.f" in cmd
    assert any(arg.startswith("-out=") and arg.endswith("tfplan") for arg in cmd)
    assert any(arg.startswith("-var-file=") for arg in cmd)
    assert isinstance(env, dict)
    assert env["ARM_SUBSCRIPTION_ID"] == "abc"
    assert env["TF_DATA_DIR"] == str(config.data_dir_for("dev"))
    assert env["TF_PLUGIN_CACHE_DIR"] == str(config.effective_plugin_cache_dir)
    assert env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] == "1"


def test_apply_uses_saved_plan_and_unlinks(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    plan_path = config.tfplan_file_for("dev")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(b"plan")
    # Companion sidecar - `tfp plan` always writes both, and preflight
    # rejects a half-deleted pair. `--force` skips the drift check itself
    # but the file's presence is what preflight asserts on.
    config.tfplan_meta_for("dev").write_text("{}")
    commands.do_apply(config, force=True)
    cmd = run_calls[0]["cmd"]
    assert "apply" in cmd
    assert str(plan_path) in cmd
    assert not plan_path.exists()


def test_destroy_targets(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
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


def test_output_requires_active_slot(config: Config) -> None:
    with pytest.raises(commands.NoActiveSlotError):
        commands.do_output(config)


def test_state_mv(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_state(config, subcommand="mv", args=["a.b", "c.d"])
    cmd = run_calls[0]["cmd"]
    assert cmd[-4:] == ["state", "mv", "a.b", "c.d"]


def test_do_state_rm(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_state(config, subcommand="rm", args=["aws_s3_bucket.foo", "aws_s3_bucket.bar"])
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert isinstance(cmd, list)
    assert "state" in cmd
    assert "rm" in cmd
    assert cmd[-2:] == ["aws_s3_bucket.foo", "aws_s3_bucket.bar"]
    assert not any(a.startswith("-var-file=") for a in cmd)
    assert isinstance(env, dict) and env["ARM_SUBSCRIPTION_ID"] == "abc"


def test_do_state_extras_before_positionals(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_state(config, subcommand="rm", args=["aws_x.foo"], extra=["-dry-run", "-lock=false"])
    cmd = run_calls[0]["cmd"]
    assert cmd.index("-dry-run") < cmd.index("aws_x.foo")
    assert cmd.index("-lock=false") < cmd.index("aws_x.foo")


def test_state_is_wrapped_not_passthrough() -> None:
    assert "state" in commands.WRAPPED_SUBCOMMANDS


def _mock_pull(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture subprocess.run calls (used by auto_snapshot)."""
    captured: list[list[str]] = []

    class R:
        stdout = b"snap"
        stderr = b""
        returncode = 0

    def fake(argv: list[str], **kw: object) -> object:
        captured.append(argv)
        return R()

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", fake)
    return captured


def test_do_state_rm_auto_snapshots_before_running(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state: Callable[..., MyState],
    run_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config.model_copy(update={"snapshot_retention": 200})
    save_dev_state()
    pull_calls = _mock_pull(monkeypatch)
    commands.do_state(cfg, subcommand="rm", args=["aws_s3_bucket.foo"])
    assert pull_calls and pull_calls[0][2:4] == ["state", "pull"], pull_calls
    snapshots = list(cfg.snapshots_dir_for("dev").glob("*.tfstate"))
    assert len(snapshots) == 1
    assert "pre-state-rm" in snapshots[0].name


def test_do_state_list_does_not_snapshot(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state: Callable[..., MyState],
    run_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config.model_copy(update={"snapshot_retention": 200})
    save_dev_state()
    pull_calls = _mock_pull(monkeypatch)
    commands.do_state(cfg, subcommand="list", args=[])
    assert pull_calls == []
    assert not cfg.snapshots_dir_for("dev").exists()


def test_do_import_auto_snapshots(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state: Callable[..., MyState],
    run_calls: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config.model_copy(update={"snapshot_retention": 200})
    save_dev_state()
    pull_calls = _mock_pull(monkeypatch)
    commands.do_import(cfg, address="aws_s3_bucket.foo", resource_id="my-bucket")
    assert pull_calls and pull_calls[0][2:4] == ["state", "pull"]
    snapshots = list(cfg.snapshots_dir_for("dev").glob("*.tfstate"))
    assert len(snapshots) == 1
    assert "pre-import" in snapshots[0].name


def test_plan_extra_args_forwarded(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_plan(config, extra=["-detailed-exitcode", "-compact-warnings"])
    cmd = run_calls[0]["cmd"]
    assert "-detailed-exitcode" in cmd
    assert "-compact-warnings" in cmd
    assert cmd.index("-detailed-exitcode") < next(i for i, a in enumerate(cmd) if a.startswith("-out="))


def test_passthrough_with_state(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
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
    cfg = config.model_copy(update={"terraform_binary": "/opt/tofu/bin/tofu"})
    commands.do_fmt(cfg)
    assert run_calls[0]["cmd"][0] == "/opt/tofu/bin/tofu"


def _write_banner_tfvars(path: pathlib.Path, banner: dict[str, object]) -> None:
    path.write_text(f'# {json.dumps(banner)}\nfoo = "bar"\n')


def test_do_use_honours_banner_state_key(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "state_key": "shared/global.tfstate"},
    )
    commands.do_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    assert state.backend_config == {"key": "shared/global.tfstate"}


def test_do_use_honours_banner_env(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _write_banner_tfvars(
        tfvars,
        {
            "header": "terraform",
            "project": "demo",
            "env": {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"},
        },
    )
    commands.do_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    assert state.environ == {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"}


def test_do_use_banner_env_replaces_prior(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"NEW_VAR": "y"}},
    )
    commands.do_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    # No merge - banner is the source of truth on every `tfp use`.
    assert state.environ == {"NEW_VAR": "y"}


def test_banner_invalid_state_key_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(tfvars, {"header": "terraform", "project": "demo", "state_key": ""})
    # Pass the path so resolution goes through find_banner directly and
    # surfaces the validation error rather than silently dropping the slot.
    with pytest.raises(ValueError, match="state_key"):
        commands.do_use(config, arg=str(tfvars))


def test_banner_invalid_env_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"OK": 123}},
    )
    with pytest.raises(ValueError, match="env"):
        commands.do_use(config, arg=str(tfvars))


def test_plan_writes_tfplan_meta(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_plan(config)
    meta = config.tfplan_meta_for("dev")
    assert meta.exists()
    payload = json.loads(meta.read_text())
    assert "inputs_sha256" in payload
    assert payload["project"] == "demo"
    assert payload["schema_version"] == 1


def test_apply_detects_changed_tf_source(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    src = config.terraform_dir / "demo"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.tf").write_text('resource "null_resource" "a" {}\n')
    commands.do_plan(config)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    (src / "main.tf").write_text('resource "null_resource" "b" {}\n')
    with pytest.raises(commands.StaleTfplanError):
        commands.do_apply(config)


def test_apply_rejects_stale_tfplan(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    config.tfplan_file_for("dev").write_bytes(b"plan")
    # Write a meta file claiming the tfplan was generated against different content.
    meta_payload = {
        "schema_version": 1,
        "inputs_sha256": "deadbeef" * 8,
        "project": "demo",
        "state_key": "terraform/azure/dev.tfstate",
        "backend_config": {"key": "terraform/azure/dev.tfstate"},
        "env": {"ARM_SUBSCRIPTION_ID": "abc"},
    }
    config.tfplan_meta_for("dev").write_text(json.dumps(meta_payload))
    with pytest.raises(commands.StaleTfplanError):
        commands.do_apply(config)
    assert config.tfplan_file_for("dev").exists()


def test_apply_force_bypasses_stale_guard(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    config.tfplan_file_for("dev").write_bytes(b"plan")
    meta_payload = {
        "schema_version": 1,
        "inputs_sha256": "wrong" * 13,
        "project": "demo",
        "state_key": "terraform/azure/dev.tfstate",
        "backend_config": {"key": "terraform/azure/dev.tfstate"},
        "env": {"ARM_SUBSCRIPTION_ID": "abc"},
    }
    config.tfplan_meta_for("dev").write_text(json.dumps(meta_payload))
    commands.do_apply(config, force=True)
    assert not config.tfplan_file_for("dev").exists()
    assert not config.tfplan_meta_for("dev").exists()


def test_apply_consumes_fresh_tfplan_meta(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_plan(config)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    commands.do_apply(config)
    assert not config.tfplan_file_for("dev").exists()
    assert not config.tfplan_meta_for("dev").exists()


def test_status_report_uninitialized(config: Config) -> None:
    report = commands.status_report(config)
    assert report.initialized is False


def test_status_report_after_init(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    report = commands.status_report(config)
    assert report.initialized is True
    assert report.slot == "dev"
    assert report.tfvars == str(tfvars)
    assert report.backend_key == "terraform/azure/dev.tfstate"
    assert "ARM_SUBSCRIPTION_ID" in report.env_keys
    assert report.plan_ready is False
    config.tfplan_file_for("dev").write_bytes(b"plan")
    assert commands.status_report(config).plan_ready is True


def test_do_use_passes_backend_config_table(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    cfg = config.model_copy(
        update={"backend_config": {"resource_group_name": "rg", "storage_account_name": "sa"}},
    )
    commands.do_use(cfg, arg="dev")
    cmd = run_calls[0]["cmd"]
    assert isinstance(cmd, list)
    flat = " ".join(cmd)
    assert "-backend-config resource_group_name=rg" in flat
    assert "-backend-config storage_account_name=sa" in flat


def test_import_forwards_var_file_and_env(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    save_dev_state()
    commands.do_import(config, address="aws_s3_bucket.foo", resource_id="my-bucket")
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert isinstance(cmd, list)
    assert "import" in cmd
    assert cmd[-2:] == ["aws_s3_bucket.foo", "my-bucket"]
    assert any(a.startswith("-var-file=") for a in cmd)
    assert isinstance(env, dict) and env["ARM_SUBSCRIPTION_ID"] == "abc"


def test_import_is_wrapped_not_passthrough() -> None:
    assert "import" in commands.WRAPPED_SUBCOMMANDS


def test_passthrough_honours_slot_flag(
    config: Config, tfvars: pathlib.Path, save_dev_state: Callable[..., MyState], run_calls: list[dict[str, object]]
) -> None:
    """do_passthrough with slot resolves env + -chdir from that slot."""
    save_dev_state()
    commands.do_passthrough(config, ["validate"], slot="dev")
    cmd = run_calls[0]["cmd"]
    env = run_calls[0]["env"]
    assert f"-chdir={config.terraform_dir / 'demo'}" in cmd
    assert isinstance(env, dict) and env["ARM_SUBSCRIPTION_ID"] == "abc"


def test_passthrough_raises_when_active_slot_has_no_state(
    config: Config,
    tfvars: pathlib.Path,
    run_calls: list[dict[str, object]],
) -> None:
    """`-s dev` for a never-`tfp use`'d slot must refuse, not silently lose -chdir."""
    with pytest.raises(commands.StateNotInitializedError):
        commands.do_passthrough(config, ["validate"], slot="dev")
    assert run_calls == []


def test_apply_skips_banner_drift_when_banner_missing(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state: Callable[..., MyState],
    run_calls: list[dict[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When the banner can no longer be resolved, apply warns rather than silently
    passing the drift check (which would be a tautology against synthesised state)."""
    from tf_project.state import save_state

    state = save_dev_state()
    commands.do_plan(config)  # writes tfplan meta
    config.tfplan_file_for("dev").write_bytes(b"plan")
    # Move the tfvars out of tfvars_dir so `list_slots` no longer resolves
    # it, but keep its content reachable so the apply path can read it and
    # the inputs-sha check still passes.
    moved = config.project_root / "elsewhere.tfvars"
    moved.write_bytes(tfvars.read_bytes())
    tfvars.unlink()
    save_state(state.model_copy(update={"tfvars": moved}), config)
    commands.do_apply(config)
    err = capsys.readouterr().err
    assert "banner" in err and "skip" in err.lower()


def test_passthrough_uses_exec(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
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


# ---- New surface: slot listing, rm ------------------------------------------


def test_slot_listing(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.do_use(config, arg="dev")
    listing = commands.slot_listing(config)
    assert [s.name for s in listing] == ["dev"]
    assert listing[0].is_active is True
    assert listing[0].project == "demo"


def test_do_rm_refuses_active(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.do_use(config, arg="dev")
    with pytest.raises(slots.SlotInUseError):
        commands.do_rm(config, name="dev", force=False)


def test_do_rm_with_force(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.do_use(config, arg="dev")
    commands.do_rm(config, name="dev", force=True)
    assert not config.slot_dir_for("dev").exists()


def test_do_rm_unknown(config: Config) -> None:
    with pytest.raises(slots.SlotNotFoundError):
        commands.do_rm(config, name="ghost", force=False)
