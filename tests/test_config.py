from __future__ import annotations

import pathlib
import textwrap

import pytest

from tf_project.config import Config, ConfigError, ConfigNotFoundError


def _write_tf_project_toml(root: pathlib.Path, body: str) -> None:
    (root / "tf_project.toml").write_text(textwrap.dedent(body))


def test_loads_from_tf_project_toml(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"
        tfvars_dir = "vars"
        tmp_dir = "build"
        state_key_prefix = "tf/azure/"
        """,
    )
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    cfg = Config.discover(nested)
    assert cfg.project_root == tmp_path.resolve()
    assert cfg.terraform_dir == (tmp_path / "infra").resolve()
    assert cfg.tfvars_dir == (tmp_path / "vars").resolve()
    assert cfg.tmp_dir == (tmp_path / "build").resolve()
    assert cfg.state_key_prefix == "tf/azure/"
    assert cfg.state_file == (tmp_path / "build" / "my_terraform_state.json").resolve()
    assert cfg.tfplan_file == (tmp_path / "build" / "my.tfplan").resolve()
    assert cfg.secrets.command[0] == "op"


def test_tf_project_toml_takes_precedence_over_pyproject(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [tool.tf_project]
            terraform_dir = "from_pyproject"
            """
        )
    )
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "from_tf_project_toml"
        """,
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_dir == (tmp_path / "from_tf_project_toml").resolve()


def test_falls_back_to_pyproject(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "consumer"

            [tool.tf_project]
            terraform_dir = "infra"
            state_key_prefix = "x/"
            """
        )
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_dir == (tmp_path / "infra").resolve()
    assert cfg.state_key_prefix == "x/"


def test_secrets_disabled_when_empty_command(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"

        [tf_project.secrets]
        command = []
        """,
    )
    cfg = Config.discover(tmp_path)
    assert cfg.secrets.command == ()


def test_missing_config_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ConfigNotFoundError):
        Config.discover(tmp_path)


def test_terraform_binary_defaults_to_which(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tf_project.config.shutil.which", lambda name: f"/fake/path/{name}")
    _write_tf_project_toml(tmp_path, "[tf_project]\nterraform_dir = 'infra'\n")
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_binary == "/fake/path/terraform"


def test_terraform_binary_defaults_to_name_when_not_on_path(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tf_project.config.shutil.which", lambda name: None)
    _write_tf_project_toml(tmp_path, "[tf_project]\nterraform_dir = 'infra'\n")
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_binary == "terraform"


def test_terraform_binary_explicit_path_resolved_relative_to_project(tmp_path: pathlib.Path) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tf").write_text("#!/bin/sh\n")
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"
        terraform_binary = "bin/tf"
        """,
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_binary == str((tmp_path / "bin" / "tf").resolve())


def test_terraform_binary_explicit_bare_name_kept(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"
        terraform_binary = "tofu"
        """,
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_binary == "tofu"


def test_backend_config_table_loaded(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"

        [tf_project.backend_config]
        resource_group_name = "rg"
        storage_account_name = "sa"
        """,
    )
    cfg = Config.discover(tmp_path)
    assert cfg.backend_config == {"resource_group_name": "rg", "storage_account_name": "sa"}


def test_backend_config_bad_type_raises(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"

        [tf_project.backend_config]
        bad = 123
        """,
    )
    with pytest.raises(ConfigError, match="backend_config"):
        Config.discover(tmp_path)


def test_bad_secrets_command_raises(tmp_path: pathlib.Path) -> None:
    _write_tf_project_toml(
        tmp_path,
        """
        [tf_project]
        terraform_dir = "infra"

        [tf_project.secrets]
        command = "op inject"
        """,
    )
    with pytest.raises(ConfigError):
        Config.discover(tmp_path)
