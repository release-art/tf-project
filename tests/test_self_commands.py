from __future__ import annotations

import dataclasses
import json
import pathlib
import tomllib

import pytest

from tf_project import self_commands
from tf_project.config import CONFIG_FILE_NAME, PYPROJECT_FILE_NAME, Config


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


def test_self_state_show_requires_state(config: Config) -> None:
    with pytest.raises(self_commands.SelfCommandError):
        self_commands.do_self_state_show(config)


def test_self_state_clear_when_present(config: Config) -> None:
    config.state_file.write_text("{}")
    assert self_commands.do_self_state_clear(config) is True
    assert not config.state_file.exists()


def test_self_state_clear_when_absent(config: Config) -> None:
    assert self_commands.do_self_state_clear(config) is False


def test_self_doctor_runs(config: Config) -> None:
    checks = self_commands.do_self_doctor(config)
    names = {c.name for c in checks}
    assert any("terraform_dir" in n for n in names)
    assert any("tfvars_dir" in n for n in names)
    assert any("tmp_dir" in n for n in names)
    assert any("terraform" in n for n in names)


def test_self_doctor_flags_missing_terraform_dir(tmp_path: pathlib.Path, config: Config) -> None:
    missing = config.terraform_dir / "does-not-exist"
    cfg = dataclasses.replace(config, terraform_dir=missing)
    checks = self_commands.do_self_doctor(cfg)
    tf_dir_check = next(c for c in checks if c.name == "terraform_dir exists")
    assert tf_dir_check.ok is False


def test_self_doctor_uses_configured_terraform_binary(tmp_path: pathlib.Path, config: Config) -> None:
    fake = tmp_path / "fake-terraform"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    cfg = dataclasses.replace(config, terraform_binary=str(fake))
    checks = self_commands.do_self_doctor(cfg)
    bin_check = next(c for c in checks if c.name.startswith("terraform binary"))
    assert bin_check.ok is True
    assert bin_check.detail == str(fake)


def test_self_doctor_flags_missing_terraform_binary_path(config: Config) -> None:
    cfg = dataclasses.replace(config, terraform_binary="/nonexistent/tf")
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
    with pytest.raises(self_commands.banner.BannerError):
        self_commands.do_self_banner_check(config, tfvars=tfvars)


# ---- Azure lock commands ------------------------------------------------------


def _save_azure_state(
    config: Config,
    tfvars: pathlib.Path,
    *,
    extra_backend: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> None:
    from tf_project.state import MyState

    bc = {
        "key": "shared/dev.tfstate",
        "storage_account_name": "tfstate0001",
        "container_name": "tfstate",
        "resource_group_name": "tfstate-rg",
    }
    if extra_backend:
        bc.update(extra_backend)
    MyState(
        tfvars=str(tfvars),
        source_root=str(config.terraform_dir / "demo"),
        tfplan_location=str(config.tfplan_file),
        environ=environ or {"ARM_SUBSCRIPTION_ID": "sub-id"},
        backend_config=bc,
    ).save(config)


def _fake_az_run(stdout: str) -> object:
    class R:
        pass

    R.stdout = stdout  # type: ignore[attr-defined]
    R.stderr = ""  # type: ignore[attr-defined]
    return R()


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

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", fake_run)
    status = self_commands.do_self_lock_status(config)
    assert status.locked is True
    assert status.lease_state == "leased"
    assert status.lease_duration == "infinite"
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
        "tf_project.self_commands.subprocess.run",
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
        "tf_project.self_commands.subprocess.run",
        lambda argv, **kw: _fake_az_run(blob_payload),
    )
    status = self_commands.do_self_lock_status(config)
    assert status.locked is True
    assert status.lock_id is None


def test_self_lock_break_shells_out_to_az(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _save_azure_state(config, tfvars)
    captured: dict[str, list[str]] = {}

    def fake_run(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        return _fake_az_run("")

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", fake_run)
    self_commands.do_self_lock_break(config)
    assert captured["argv"][:5] == ["az", "storage", "blob", "lease", "break"]
    assert "--blob-name" in captured["argv"]
    assert "shared/dev.tfstate" in captured["argv"]


def test_self_lock_requires_state(config: Config) -> None:
    with pytest.raises(self_commands.SelfCommandError, match="No state"):
        self_commands.do_self_lock_status(config)


def test_self_lock_requires_azure_metadata(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tf_project.state import MyState

    MyState(
        tfvars=str(tfvars),
        source_root=str(config.terraform_dir / "demo"),
        tfplan_location=str(config.tfplan_file),
        environ={},
        backend_config={"key": "foo.tfstate"},  # only key; missing account + container
    ).save(config)
    with pytest.raises(self_commands.SelfCommandError, match="storage_account_name"):
        self_commands.do_self_lock_status(config)


def test_self_lock_handles_missing_az(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _save_azure_state(config, tfvars)

    def boom(*a: object, **kw: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="Azure CLI"):
        self_commands.do_self_lock_status(config)


def test_self_lock_handles_az_failure(config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    _save_azure_state(config, tfvars)

    def boom(argv: list[str], **kw: object) -> object:
        raise subprocess.CalledProcessError(returncode=1, cmd=argv, output="", stderr="not authorized\n")

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", boom)
    with pytest.raises(self_commands.SelfCommandError, match="not authorized"):
        self_commands.do_self_lock_break(config)
