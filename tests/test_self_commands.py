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
