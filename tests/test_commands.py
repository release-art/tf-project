from __future__ import annotations

import json
import pathlib

import pytest

from tf_project import commands
from tf_project.config import Config
from tf_project.models import MyState
from tf_project.state import load_state, save_state, write_active


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        calls.append({"cmd": cmd, "env": env})

    monkeypatch.setattr("tf_project.commands.terraform.run", fake_run)
    monkeypatch.setattr("tf_project.commands.terraform.exec_passthrough", fake_run)
    return calls


def _save_dev_state(config: Config, *, tfvars: pathlib.Path) -> MyState:
    state = MyState(
        slot="dev",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={"ARM_SUBSCRIPTION_ID": "abc"},
        backend_config={"key": "terraform/azure/dev.tfstate"},
    )
    save_state(state, config)
    write_active(config, "dev")
    return state


def test_cmd_use_runs_init_and_persists_state(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    commands.cmd_use(config, arg="dev")
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
    assert config.state_file_for("dev").exists()
    assert config.active_file.read_text().strip() == "dev"


def test_cmd_use_warm_skips_init(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    commands.cmd_use(config, arg="dev")
    assert len(run_calls) == 1
    # Simulate terraform having written its data-dir marker so the warmth
    # check believes init has run.
    (config.data_dir_for("dev")).mkdir(parents=True, exist_ok=True)
    (config.data_dir_for("dev") / "terraform.tfstate").write_text("{}")
    run_calls.clear()
    commands.cmd_use(config, arg="dev")
    assert run_calls == []


def test_cmd_use_by_path(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    commands.cmd_use(config, arg=str(tfvars))
    assert config.state_file_for("dev").exists()


def test_cmd_use_init_failure_wipes_slot_dir(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project import terraform as terraform_mod

    def fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        raise terraform_mod.TerraformExit(2)

    monkeypatch.setattr("tf_project.commands.terraform.run", fake_run)
    with pytest.raises(terraform_mod.TerraformExit):
        commands.cmd_use(config, arg="dev")
    assert not config.slot_dir_for("dev").exists()


def test_plan_targets_and_replaces(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
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
    assert env["TF_PLUGIN_CACHE_DIR"] == str(config.default_plugin_cache_dir)
    assert env["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] == "1"


def test_apply_uses_saved_plan_and_unlinks(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    plan_path = config.tfplan_file_for("dev")
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(b"plan")
    commands.do_apply(config)
    cmd = run_calls[0]["cmd"]
    assert "apply" in cmd
    assert str(plan_path) in cmd
    assert not plan_path.exists()


def test_destroy_targets(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_dev_state(config, tfvars=tfvars)
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


def test_state_mv(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_dev_state(config, tfvars=tfvars)
    commands.do_state(config, subcommand="mv", args=["a.b", "c.d"])
    cmd = run_calls[0]["cmd"]
    assert cmd[-4:] == ["state", "mv", "a.b", "c.d"]


def test_do_state_rm(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    _save_dev_state(config, tfvars=tfvars)
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
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    commands.do_state(config, subcommand="rm", args=["aws_x.foo"], extra=["-dry-run", "-lock=false"])
    cmd = run_calls[0]["cmd"]
    assert cmd.index("-dry-run") < cmd.index("aws_x.foo")
    assert cmd.index("-lock=false") < cmd.index("aws_x.foo")


def test_state_is_wrapped_not_passthrough() -> None:
    assert "state" in commands.WRAPPED_SUBCOMMANDS


def test_plan_extra_args_forwarded(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    commands.do_plan(config, extra=["-detailed-exitcode", "-compact-warnings"])
    cmd = run_calls[0]["cmd"]
    assert "-detailed-exitcode" in cmd
    assert "-compact-warnings" in cmd
    assert cmd.index("-detailed-exitcode") < next(i for i, a in enumerate(cmd) if a.startswith("-out="))


def test_passthrough_with_state(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
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


def test_cmd_use_honours_banner_state_key(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "state_key": "shared/global.tfstate"},
    )
    commands.cmd_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    assert state.backend_config == {"key": "shared/global.tfstate"}


def test_cmd_use_honours_banner_env(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _write_banner_tfvars(
        tfvars,
        {
            "header": "terraform",
            "project": "demo",
            "env": {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"},
        },
    )
    commands.cmd_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    assert state.environ == {"ARM_SUBSCRIPTION_ID": "from-banner", "EXTRA": "x"}


def test_cmd_use_banner_env_replaces_prior(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"NEW_VAR": "y"}},
    )
    commands.cmd_use(config, arg="dev")
    state = load_state(config, "dev")
    assert state is not None
    # No merge — banner is the source of truth on every `tfp use`.
    assert state.environ == {"NEW_VAR": "y"}


def test_banner_invalid_state_key_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(tfvars, {"header": "terraform", "project": "demo", "state_key": ""})
    # Pass the path so resolution goes through find_banner directly and
    # surfaces the validation error rather than silently dropping the slot.
    with pytest.raises(ValueError, match="state_key"):
        commands.cmd_use(config, arg=str(tfvars))


def test_banner_invalid_env_raises(config: Config, tfvars: pathlib.Path) -> None:
    _write_banner_tfvars(
        tfvars,
        {"header": "terraform", "project": "demo", "env": {"OK": 123}},
    )
    with pytest.raises(ValueError, match="env"):
        commands.cmd_use(config, arg=str(tfvars))


def test_plan_writes_tfplan_meta(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    commands.do_plan(config)
    meta = config.tfplan_meta_for("dev")
    assert meta.exists()
    payload = json.loads(meta.read_text())
    assert "inputs_sha256" in payload
    assert payload["project"] == "demo"
    assert payload["schema_version"] == 1


def test_apply_detects_changed_tf_source(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    src = config.terraform_dir / "demo"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.tf").write_text('resource "null_resource" "a" {}\n')
    commands.do_plan(config)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    (src / "main.tf").write_text('resource "null_resource" "b" {}\n')
    with pytest.raises(commands.StaleTfplanError):
        commands.do_apply(config)


def test_apply_rejects_stale_tfplan(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
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
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
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
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    commands.do_plan(config)
    config.tfplan_file_for("dev").write_bytes(b"plan")
    commands.do_apply(config)
    assert not config.tfplan_file_for("dev").exists()
    assert not config.tfplan_meta_for("dev").exists()


def test_status_report_uninitialized(config: Config) -> None:
    report = commands.status_report(config)
    assert report.initialized is False


def test_status_report_after_init(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
    report = commands.status_report(config)
    assert report.initialized is True
    assert report.slot == "dev"
    assert report.tfvars == str(tfvars)
    assert report.backend_key == "terraform/azure/dev.tfstate"
    assert "ARM_SUBSCRIPTION_ID" in report.env_keys
    assert report.plan_ready is False
    config.tfplan_file_for("dev").write_bytes(b"plan")
    assert commands.status_report(config).plan_ready is True


def test_cmd_use_passes_backend_config_table(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    cfg = config.model_copy(
        update={"backend_config": {"resource_group_name": "rg", "storage_account_name": "sa"}},
    )
    commands.cmd_use(cfg, arg="dev")
    cmd = run_calls[0]["cmd"]
    assert isinstance(cmd, list)
    flat = " ".join(cmd)
    assert "-backend-config resource_group_name=rg" in flat
    assert "-backend-config storage_account_name=sa" in flat


def test_import_forwards_var_file_and_env(
    config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]
) -> None:
    _save_dev_state(config, tfvars=tfvars)
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
    commands.cmd_use(config, arg="dev")
    listing = commands.slot_listing(config)
    assert [s.name for s in listing] == ["dev"]
    assert listing[0].is_active is True
    assert listing[0].project == "demo"


def test_cmd_rm_refuses_active(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.cmd_use(config, arg="dev")
    with pytest.raises(commands.SlotInUseError):
        commands.cmd_rm(config, slot="dev", force=False)


def test_cmd_rm_with_force(config: Config, tfvars: pathlib.Path, run_calls: list[dict[str, object]]) -> None:
    commands.cmd_use(config, arg="dev")
    commands.cmd_rm(config, slot="dev", force=True)
    assert not config.slot_dir_for("dev").exists()


def test_cmd_rm_unknown(config: Config) -> None:
    with pytest.raises(commands.SlotNotFoundError):
        commands.cmd_rm(config, slot="ghost", force=False)
