from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from tf_project import self_commands
from tf_project.config import CONFIG_FILE_NAME, PYPROJECT_FILE_NAME, Config
from tf_project.models import MyState
from tf_project.state import save_state, write_active


def test_self_init_creates_standalone_when_no_pyproject(tmp_path: pathlib.Path) -> None:
    written = self_commands.do_self_init(tmp_path)
    assert written == tmp_path / CONFIG_FILE_NAME
    cfg = Config.discover(tmp_path)
    assert cfg.project_root == tmp_path.resolve()
    assert cfg.secrets.command[0] == "op"


def test_self_init_appends_to_pyproject(tmp_path: pathlib.Path) -> None:
    pyproject = tmp_path / PYPROJECT_FILE_NAME
    pyproject.write_text('[project]\nname = "consumer"\n')
    written = self_commands.do_self_init(tmp_path)
    assert written == pyproject
    with pyproject.open("rb") as fin:
        data = tomllib.load(fin)
    assert data["project"]["name"] == "consumer"
    assert "tf_project" in data["tool"]
    assert "terraform_dir" in data["tool"]["tf_project"]


def test_self_init_refuses_to_overwrite_standalone(tmp_path: pathlib.Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("[tf_project]\n")
    with pytest.raises(self_commands.SelfCommandError):
        self_commands.do_self_init(tmp_path)


def test_self_init_refuses_to_overwrite_pyproject_section(tmp_path: pathlib.Path) -> None:
    (tmp_path / PYPROJECT_FILE_NAME).write_text("[tool.tf_project]\nterraform_dir = 'x'\n")
    with pytest.raises(self_commands.SelfCommandError):
        self_commands.do_self_init(tmp_path)


def test_self_config_print_json_roundtrips(config: Config) -> None:
    out = self_commands.do_self_config_print(config, as_json=True)
    payload = json.loads(out)
    assert payload["project_root"] == str(config.project_root)
    assert payload["terraform_dir"] == str(config.terraform_dir)
    assert payload["secrets"]["command"] == []


def test_self_config_print_human(config: Config) -> None:
    out = self_commands.do_self_config_print(config, as_json=False)
    assert "terraform_dir" in out
    assert str(config.terraform_dir) in out


def test_self_config_path_finds_standalone(tmp_path: pathlib.Path) -> None:
    (tmp_path / CONFIG_FILE_NAME).write_text("[tf_project]\nterraform_dir = 'x'\n")
    assert self_commands.do_self_config_path(tmp_path) == tmp_path / CONFIG_FILE_NAME


def test_self_config_path_finds_pyproject(tmp_path: pathlib.Path) -> None:
    (tmp_path / PYPROJECT_FILE_NAME).write_text("[tool.tf_project]\nterraform_dir = 'x'\n")
    assert self_commands.do_self_config_path(tmp_path) == tmp_path / PYPROJECT_FILE_NAME


def test_self_state_show_requires_active_slot(config: Config) -> None:
    with pytest.raises(self_commands.SelfCommandError):
        self_commands.do_self_state_show(config, slot=None, all_slots=False)


def test_self_state_show_active(config: Config, save_dev_state) -> None:  # type: ignore[no-untyped-def]
    save_dev_state()
    out = self_commands.do_self_state_show(config, slot=None, all_slots=False)
    payload = json.loads(out)
    assert payload["slot"] == "dev"


def test_self_state_clear_when_present(config: Config, save_dev_state) -> None:  # type: ignore[no-untyped-def]
    save_dev_state()
    removed = self_commands.do_self_state_clear(config, slot="dev", all_slots=False)
    assert removed == [config.state_file_for("dev")]
    assert not config.state_file_for("dev").exists()


def test_self_state_clear_when_absent(config: Config) -> None:
    write_active(config, "dev")  # have an active slot to resolve
    assert self_commands.do_self_state_clear(config, slot="dev", all_slots=False) == []


def test_self_doctor_runs(config: Config) -> None:
    checks = self_commands.do_self_doctor(config)
    names = {c.name for c in checks}
    assert any("terraform_dir" in n for n in names)
    assert any("tfvars_dir" in n for n in names)
    assert any("tmp_dir" in n for n in names)
    assert any("terraform" in n for n in names)
    assert any("uniqueness" in n for n in names)
    assert any("orphan" in n for n in names)


def test_self_doctor_flags_missing_terraform_dir(tmp_path: pathlib.Path, config: Config) -> None:
    missing = config.terraform_dir / "does-not-exist"
    cfg = config.model_copy(update={"terraform_dir": missing})
    checks = self_commands.do_self_doctor(cfg)
    tf_dir_check = next(c for c in checks if c.name == "terraform_dir exists")
    assert tf_dir_check.ok is False


def test_self_doctor_uses_configured_terraform_binary(tmp_path: pathlib.Path, config: Config) -> None:
    fake = tmp_path / "fake-terraform"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    cfg = config.model_copy(update={"terraform_binary": str(fake)})
    checks = self_commands.do_self_doctor(cfg)
    bin_check = next(c for c in checks if c.name.startswith("terraform binary"))
    assert bin_check.ok is True
    assert bin_check.detail == str(fake)


def test_self_doctor_flags_missing_terraform_binary_path(config: Config) -> None:
    cfg = config.model_copy(update={"terraform_binary": "/nonexistent/tf"})
    checks = self_commands.do_self_doctor(cfg)
    bin_check = next(c for c in checks if c.name.startswith("terraform binary"))
    assert bin_check.ok is False


def test_self_banner_check_summary(config: Config, tfvars: pathlib.Path) -> None:
    tfvars.write_text('# {"header":"terraform","project":"demo","env":{"A":"1"},"state_key":"k"}\nfoo = "bar"\n')
    summary = self_commands.do_self_banner_check(config, tfvars=tfvars)
    assert summary["project"] == "demo"
    assert summary["env"] == {"A": "1"}
    assert summary["backend_config"] == {"key": "k"}
    assert summary["source_root"].endswith("/terraform/demo")


def test_self_banner_check_raises_on_invalid(config: Config, tfvars: pathlib.Path) -> None:
    tfvars.write_text('# {"header":"terraform","project":""}\nfoo = "bar"\n')
    from tf_project import banner as banner_mod

    with pytest.raises(banner_mod.BannerError):
        self_commands.do_self_banner_check(config, tfvars=tfvars)


# ---- Helpers / Azure lock ----------------------------------------------------
#
# Note: the generic "dev" state factory is the `save_dev_state` fixture in
# conftest.py. The azure/s3 helpers below are kept local because they encode
# backend-specific default backend_config — too noisy for the shared fixture.


def _save_azure_state(
    config: Config,
    tfvars: pathlib.Path,
    *,
    extra_backend: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> None:
    bc = {
        "key": "shared/dev.tfstate",
        "storage_account_name": "tfstate0001",
        "container_name": "tfstate",
        "resource_group_name": "tfstate-rg",
    }
    if extra_backend:
        bc.update(extra_backend)
    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ=environ or {"ARM_SUBSCRIPTION_ID": "sub-id"},
        backend_config=bc,
    )
    save_state(state, config)
    write_active(config, "dev")


def _fake_az_run(stdout: str) -> object:
    class R:
        pass

    R.stdout = stdout  # type: ignore[attr-defined]
    R.stderr = ""  # type: ignore[attr-defined]
    return R()


@pytest.mark.parametrize(
    "subcommand",
    ["plan", "apply", "refresh", "destroy", "output", "import"],
)
def test_self_trace_arms(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,  # type: ignore[no-untyped-def]
    subcommand: str,
) -> None:
    """Every match arm in do_self_trace returns a valid argv with `terraform` first."""
    save_dev_state()
    argv = self_commands.do_self_trace(config, subcommand=subcommand)
    assert argv[0] == config.terraform_binary


def test_self_trace_requires_state_for_non_use_subcommands(config: Config, tfvars: pathlib.Path) -> None:
    """Subcommands other than `use` need saved state."""
    from tf_project.state import write_active

    write_active(config, "dev")
    with pytest.raises(self_commands.SelfCommandError, match="No state"):
        self_commands.do_self_trace(config, subcommand="plan")


def test_self_trace_unknown_subcommand(config: Config) -> None:
    with pytest.raises(self_commands.SelfCommandError, match="Unknown subcommand"):
        self_commands.do_self_trace(config, subcommand="bogus")


def test_self_trace_use_missing_banner(config: Config, tfvars: pathlib.Path) -> None:
    """`trace use` surfaces SlotError when no slot resolves."""
    from tf_project.state import write_active

    write_active(config, "ghost")
    with pytest.raises(self_commands.SelfCommandError):
        self_commands.do_self_trace(config, subcommand="use", slot="ghost")


def test_self_state_show_all(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,  # type: ignore[no-untyped-def]
) -> None:
    """`--all` shows a map of every banner-defined slot."""
    save_dev_state()
    out = self_commands.do_self_state_show(config, slot=None, all_slots=True)
    payload = json.loads(out)
    assert "dev" in payload
    assert payload["dev"]["slot"] == "dev"


def test_self_state_clear_all(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    removed = self_commands.do_self_state_clear(config, slot=None, all_slots=True)
    assert config.state_file_for("dev") in removed
    assert not config.state_file_for("dev").exists()


def test_resolve_slot_name_no_active(config: Config) -> None:
    """`_resolve_slot_name` adapter re-raises NoActiveSlotError as SelfCommandError."""
    with pytest.raises(self_commands.SelfCommandError, match="No active slot"):
        self_commands._resolve_slot_name(config, slot=None)


def test_require_state_or_error_adapter(config: Config, tfvars: pathlib.Path) -> None:
    """`_require_state_or_error` re-raises StateNotInitializedError as SelfCommandError."""
    with pytest.raises(self_commands.SelfCommandError):
        self_commands._require_state_or_error(config, "dev")


def test_self_snapshot_writes_to_default_dest(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`do_self_snapshot` writes the stdout of `terraform state pull` to dest."""

    save_dev_state()

    class R:
        stdout = b"snapshot-bytes"
        stderr = b""
        returncode = 0

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", lambda *a, **kw: R())
    dest = self_commands.do_self_snapshot(config)
    assert dest.read_bytes() == b"snapshot-bytes"
    assert dest.name.startswith("snapshot-") and dest.name.endswith(".tfstate")


def test_self_snapshot_terraform_missing(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()

    def boom(*a: object, **kw: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="not found"):
        self_commands.do_self_snapshot(config)


def test_self_snapshot_terraform_failure(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    import subprocess

    save_dev_state()

    def boom(*a: object, **kw: object) -> object:
        raise subprocess.CalledProcessError(returncode=1, cmd=a[0], output=b"", stderr=b"auth error\n")

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="auth error"):
        self_commands.do_self_snapshot(config)


def test_self_providers_lock_default_platforms(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    calls: list[list[str]] = []
    monkeypatch.setattr("tf_project.terraform.run", lambda cmd, env=None: calls.append(cmd))
    argv = self_commands.do_self_providers_lock(config)
    assert any("-platform=linux_amd64" in a for a in argv)
    assert any("-platform=darwin_arm64" in a for a in argv)
    assert calls and calls[0] == argv


def test_self_providers_lock_custom_platforms(
    config: Config,
    tfvars: pathlib.Path,
    save_dev_state,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    save_dev_state()
    monkeypatch.setattr("tf_project.terraform.run", lambda cmd, env=None: None)
    argv = self_commands.do_self_providers_lock(config, platforms=["linux_amd64"])
    assert "-platform=linux_amd64" in argv
    assert not any("darwin" in a for a in argv)


def test_self_trace_use_does_not_require_saved_state(config: Config, tfvars: pathlib.Path) -> None:
    """`tfp self trace use` should work before the slot has ever been init'd."""
    from tf_project.state import write_active

    # Slot is known via the banner but has never been `tfp use`'d → no state.json.
    write_active(config, "dev")
    argv = self_commands.do_self_trace(config, subcommand="use", slot="dev")
    assert "init" in argv
    assert "-reconfigure" in argv


def test_self_lock_status_parses_az_output(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    _save_azure_state(config, tfvars)
    captured: dict[str, list[str]] = {}

    lock_info = {
        "ID": "12345678-90ab-cdef-1234-567890abcdef",
        "Operation": "OperationTypePlan",
        "Who": "user@host",
        "Created": "2026-05-14T12:00:00Z",
    }
    metadata_value = base64.b64encode(json.dumps(lock_info).encode()).decode()
    blob_payload = json.dumps(
        {
            "properties": {"lease": {"status": "locked", "state": "leased", "duration": "infinite"}},
            "metadata": {"terraformlockid": metadata_value},
        }
    )

    def fake_run(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        return _fake_az_run(blob_payload)

    monkeypatch.setattr("tf_project.lock.subprocess.run", fake_run)
    status = self_commands.do_self_lock_status(config)
    assert status.backend == "azurerm"
    assert status.locked is True
    assert "lease_state=leased" in status.detail
    assert "lease_duration=infinite" in status.detail
    assert status.lock_id == "12345678-90ab-cdef-1234-567890abcdef"
    assert status.lock_who == "user@host"
    assert status.lock_operation == "OperationTypePlan"
    assert captured["argv"][:4] == ["az", "storage", "blob", "show"]
    assert "--account-name" in captured["argv"]
    assert "tfstate0001" in captured["argv"]
    assert "--blob-name" in captured["argv"]
    assert "shared/dev.tfstate" in captured["argv"]
    assert "--subscription" in captured["argv"]
    assert "sub-id" in captured["argv"]


def test_self_lock_status_handles_missing_metadata(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_azure_state(config, tfvars)
    blob_payload = json.dumps({"properties": {"lease": {"status": "unlocked", "state": "available"}}, "metadata": {}})
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )
    status = self_commands.do_self_lock_status(config)
    assert status.locked is False
    assert status.lock_id is None
    assert status.lock_who is None


def test_self_lock_status_handles_unparseable_metadata(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_azure_state(config, tfvars)
    blob_payload = json.dumps(
        {
            "properties": {"lease": {"status": "locked", "state": "leased"}},
            "metadata": {"terraformlockid": "not-base64-!@#$"},
        }
    )
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )
    status = self_commands.do_self_lock_status(config)
    assert status.locked is True
    assert status.lock_id is None


def test_self_lock_break_polite_path_uses_force_unlock(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    _save_azure_state(config, tfvars)
    lock_info = {"ID": "abc-id", "Operation": "OperationTypePlan", "Who": "x", "Created": "y"}
    metadata_value = base64.b64encode(json.dumps(lock_info).encode()).decode()
    blob_payload = json.dumps(
        {
            "properties": {"lease": {"status": "locked", "state": "leased"}},
            "metadata": {"terraformlockid": metadata_value},
        }
    )
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )
    tf_run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "tf_project.terraform.run",
        lambda cmd, env=None: tf_run_calls.append(cmd),
    )
    result = self_commands.do_self_lock_break(config)
    assert result.method == "force-unlock"
    assert result.lock_id == "abc-id"
    assert tf_run_calls and "force-unlock" in tf_run_calls[0]
    assert "abc-id" in tf_run_calls[0]


def test_self_lock_break_refuses_when_no_lock_present(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polite break must NOT escalate to backend-level when there's nothing to break."""
    _save_azure_state(config, tfvars)
    blob_payload = json.dumps(
        {
            "properties": {"lease": {"status": "unlocked", "state": "available"}},
            "metadata": {},  # no terraformlockid → no lock
        }
    )
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )
    with pytest.raises(self_commands.SelfCommandError, match="No lock to break"):
        self_commands.do_self_lock_break(config)


def test_self_lock_break_blunt_skips_force_unlock(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_azure_state(config, tfvars)
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kw: object) -> object:
        captured.setdefault("calls", [])
        captured["calls"].append(argv)  # type: ignore[arg-type]
        return _fake_az_run("")

    monkeypatch.setattr("tf_project.lock.subprocess.run", fake_run)
    tf_run_called = []
    monkeypatch.setattr(
        "tf_project.terraform.run",
        lambda cmd, env=None: tf_run_called.append(cmd),
    )
    result = self_commands.do_self_lock_break(config, blunt=True)
    assert result.method == "lease-break"
    assert result.backend == "azurerm"
    assert tf_run_called == []
    assert captured["calls"][0][:5] == ["az", "storage", "blob", "lease", "break"]


def test_self_lock_break_falls_back_when_terraform_fails(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from tf_project import terraform as terraform_mod

    _save_azure_state(config, tfvars)
    lock_info = {"ID": "abc-id"}
    metadata_value = base64.b64encode(json.dumps(lock_info).encode()).decode()
    blob_payload = json.dumps(
        {
            "properties": {"lease": {"status": "locked", "state": "leased"}},
            "metadata": {"terraformlockid": metadata_value},
        }
    )
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )

    def boom(cmd: list[str], env: dict[str, str] | None = None) -> None:
        raise terraform_mod.TerraformExit(1)

    monkeypatch.setattr("tf_project.terraform.run", boom)
    result = self_commands.do_self_lock_break(config)
    assert result.method == "lease-break"


def test_self_lock_requires_state(config: Config) -> None:
    write_active(config, "dev")
    with pytest.raises(self_commands.SelfCommandError, match="No state"):
        self_commands.do_self_lock_status(config)


def test_self_lock_requires_known_backend(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={"key": "foo.tfstate"},
    )
    save_state(state, config)
    write_active(config, "dev")
    with pytest.raises(self_commands.SelfCommandError, match="Unrecognised backend"):
        self_commands.do_self_lock_status(config)


def test_self_lock_handles_missing_az(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_azure_state(config, tfvars)

    def boom(*a: object, **kw: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr("tf_project.lock.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="not found"):
        self_commands.do_self_lock_status(config)


def test_self_lock_handles_az_failure(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    _save_azure_state(config, tfvars)

    def boom(argv: list[str], **kw: object) -> object:
        raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="not authorized\n")

    monkeypatch.setattr("tf_project.lock.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="not authorized"):
        self_commands.do_self_lock_break(config, blunt=True)


# ---- S3 + DynamoDB lock provider ----------------------------------------------


def _save_s3_state(
    config: Config,
    tfvars: pathlib.Path,
    *,
    extra_backend: dict[str, str] | None = None,
) -> None:
    bc = {
        "key": "envs/dev/terraform.tfstate",
        "bucket": "my-tfstate-bucket",
        "dynamodb_table": "tfstate-locks",
        "region": "us-east-1",
    }
    if extra_backend:
        bc.update(extra_backend)
    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config=bc,
    )
    save_state(state, config)
    write_active(config, "dev")


def test_s3_lock_status_locked(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_s3_state(config, tfvars)
    captured: dict[str, list[str]] = {}

    info_json = json.dumps({"ID": "aws-lock-id", "Operation": "OperationTypePlan", "Who": "ec2-user@host"})
    dynamo_payload = json.dumps({"Item": {"LockID": {"S": "x"}, "Info": {"S": info_json}}})

    def fake_run(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        return _fake_az_run(dynamo_payload)

    monkeypatch.setattr("tf_project.lock.subprocess.run", fake_run)
    status = self_commands.do_self_lock_status(config)
    assert status.backend == "s3"
    assert status.locked is True
    assert status.lock_id == "aws-lock-id"
    assert captured["argv"][:3] == ["aws", "dynamodb", "get-item"]
    assert "--table-name" in captured["argv"]
    assert "tfstate-locks" in captured["argv"]
    assert "--region" in captured["argv"]
    assert "us-east-1" in captured["argv"]


def test_s3_lock_status_unlocked(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_s3_state(config, tfvars)
    monkeypatch.setattr(
        "tf_project.lock.subprocess.run",
        lambda argv, **kw: _fake_az_run("{}"),
    )
    status = self_commands.do_self_lock_status(config)
    assert status.locked is False
    assert status.lock_id is None


def test_s3_lock_break_calls_delete_item(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_s3_state(config, tfvars)
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        return _fake_az_run("")

    monkeypatch.setattr("tf_project.lock.subprocess.run", fake_run)
    self_commands.do_self_lock_break(config, blunt=True)
    assert captured["argv"][:3] == ["aws", "dynamodb", "delete-item"]
    assert "tfstate-locks" in captured["argv"]


def test_s3_without_dynamodb_table_rejected(config: Config, tfvars: pathlib.Path) -> None:
    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={"key": "k", "bucket": "b"},
    )
    save_state(state, config)
    write_active(config, "dev")
    with pytest.raises(self_commands.SelfCommandError, match="dynamodb_table"):
        self_commands.do_self_lock_status(config)
