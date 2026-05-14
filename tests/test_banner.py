"""Tests for tfvars-banner parsing and validation."""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from tf_project import banner
from tf_project.config import Config


def _write(tfvars: pathlib.Path, info: dict[str, object]) -> None:
    tfvars.write_text(f'# {json.dumps(info)}\nfoo = "bar"\n')


def test_find_project_info_returns_banner(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo"})
    assert banner.find_project_info(tfvars)["project"] == "demo"


def test_find_project_info_raises_when_missing(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    tfvars.write_text('# not json\nfoo = "bar"\n')
    with pytest.raises(banner.ProjectInfoNotFoundError):
        banner.find_project_info(tfvars)


def test_parse_project_rejects_missing(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    with pytest.raises(banner.BannerError):
        banner.parse_project({}, tfvars=tfvars)


def test_parse_env_validates_types(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    with pytest.raises(banner.BannerError, match="string keys"):
        banner.parse_env({"env": {"OK": 1}}, tfvars=tfvars)


def test_parse_backend_config_validates_types(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    with pytest.raises(banner.BannerError, match="backend_config"):
        banner.parse_backend_config({"backend_config": {"k": 1}}, tfvars=tfvars)


def test_resolve_backend_config_synthesises_key(config: Config, tfvars: pathlib.Path) -> None:
    info = {"header": "terraform", "project": "demo"}
    out = banner.resolve_backend_config(info, tfvars=tfvars, config=config)
    assert out == {"key": "terraform/azure/dev.tfstate"}


def test_resolve_backend_config_no_prefix_no_key(config: Config, tfvars: pathlib.Path) -> None:
    cfg = dataclasses.replace(config, state_key_prefix="")
    info = {"header": "terraform", "project": "demo"}
    out = banner.resolve_backend_config(info, tfvars=tfvars, config=cfg)
    assert out == {}


def test_resolve_backend_config_config_level_merged(config: Config, tfvars: pathlib.Path) -> None:
    cfg = dataclasses.replace(
        config,
        backend_config={"resource_group_name": "rg", "storage_account_name": "sa"},
    )
    info = {"header": "terraform", "project": "demo"}
    out = banner.resolve_backend_config(info, tfvars=tfvars, config=cfg)
    assert out["resource_group_name"] == "rg"
    assert out["storage_account_name"] == "sa"
    assert out["key"] == "terraform/azure/dev.tfstate"


def test_resolve_backend_config_banner_wins_over_config(config: Config, tfvars: pathlib.Path) -> None:
    cfg = dataclasses.replace(config, backend_config={"resource_group_name": "from-config"})
    info = {
        "header": "terraform",
        "project": "demo",
        "backend_config": {"resource_group_name": "from-banner"},
    }
    out = banner.resolve_backend_config(info, tfvars=tfvars, config=cfg)
    assert out["resource_group_name"] == "from-banner"


def test_resolve_backend_config_state_key_wins(config: Config, tfvars: pathlib.Path) -> None:
    info = {
        "header": "terraform",
        "project": "demo",
        "backend_config": {"key": "from-bc"},
        "state_key": "from-state-key",
    }
    out = banner.resolve_backend_config(info, tfvars=tfvars, config=config)
    assert out["key"] == "from-state-key"


def test_render_summary(config: Config, tfvars: pathlib.Path) -> None:
    info = {
        "header": "terraform",
        "project": "demo",
        "env": {"A": "1"},
        "backend_config": {"resource_group_name": "rg"},
    }
    summary = banner.render_summary(info, tfvars=tfvars, config=config)
    assert summary["project"] == "demo"
    assert summary["env"] == {"A": "1"}
    assert summary["backend_config"]["resource_group_name"] == "rg"
    assert summary["backend_config"]["key"] == "terraform/azure/dev.tfstate"
