"""Tests for tfvars-banner parsing and validation."""

from __future__ import annotations

import json
import pathlib

import pytest

from tf_project import banner
from tf_project.config import Config
from tf_project.models import Banner


def _write(tfvars: pathlib.Path, info: dict[str, object]) -> None:
    tfvars.write_text(f'# {json.dumps(info)}\nfoo = "bar"\n')


def test_find_banner_returns_parsed_model(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo"})
    b = banner.find_banner(tfvars)
    assert isinstance(b, Banner)
    assert b.project == "demo"
    assert b.slot is None


def test_find_banner_raises_when_missing(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    tfvars.write_text('# not json\nfoo = "bar"\n')
    with pytest.raises(banner.ProjectInfoNotFoundError):
        banner.find_banner(tfvars)


def test_banner_rejects_missing_project(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform"})
    with pytest.raises(banner.BannerError):
        banner.find_banner(tfvars)


def test_banner_rejects_non_string_env_values(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo", "env": {"OK": 1}})
    with pytest.raises(banner.BannerError):
        banner.find_banner(tfvars)


def test_banner_rejects_non_string_backend_config_values(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo", "backend_config": {"k": 1}})
    with pytest.raises(banner.BannerError):
        banner.find_banner(tfvars)


def test_banner_allows_extra_fields_and_warns(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo", "future_field": "v"})
    b = banner.find_banner(tfvars)
    assert b.project == "demo"
    err = capsys.readouterr().err
    assert "future_field" in err
    assert str(tfvars) in err


def test_banner_no_extras_no_warning(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo"})
    banner.find_banner(tfvars)
    assert capsys.readouterr().err == ""


def test_banner_slot_field_parsed(tmp_path: pathlib.Path) -> None:
    tfvars = tmp_path / "a.tfvars"
    _write(tfvars, {"header": "terraform", "project": "demo", "slot": "custom"})
    b = banner.find_banner(tfvars)
    assert b.slot == "custom"


def test_resolve_backend_config_synthesises_key(config: Config, tfvars: pathlib.Path) -> None:
    b = Banner.model_validate({"header": "terraform", "project": "demo"})
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=config)
    assert out == {"key": "terraform/azure/dev.tfstate"}


def test_resolve_backend_config_no_prefix_no_key(config: Config, tfvars: pathlib.Path) -> None:
    cfg = config.model_copy(update={"state_key_prefix": ""})
    b = Banner.model_validate({"header": "terraform", "project": "demo"})
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=cfg)
    assert out == {}


def test_resolve_backend_config_config_level_merged(config: Config, tfvars: pathlib.Path) -> None:
    cfg = config.model_copy(
        update={"backend_config": {"resource_group_name": "rg", "storage_account_name": "sa"}},
    )
    b = Banner.model_validate({"header": "terraform", "project": "demo"})
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=cfg)
    assert out["resource_group_name"] == "rg"
    assert out["storage_account_name"] == "sa"
    assert out["key"] == "terraform/azure/dev.tfstate"


def test_resolve_backend_config_banner_wins_over_config(config: Config, tfvars: pathlib.Path) -> None:
    cfg = config.model_copy(update={"backend_config": {"resource_group_name": "from-config"}})
    b = Banner.model_validate(
        {
            "header": "terraform",
            "project": "demo",
            "backend_config": {"resource_group_name": "from-banner"},
        }
    )
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=cfg)
    assert out["resource_group_name"] == "from-banner"


def test_resolve_backend_config_state_key_wins(config: Config, tfvars: pathlib.Path) -> None:
    b = Banner.model_validate(
        {
            "header": "terraform",
            "project": "demo",
            "backend_config": {"key": "from-bc"},
            "state_key": "/from-state-key",
        }
    )
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=config)
    assert out["key"] == "from-state-key"


@pytest.mark.parametrize(
    ("state_key", "expected"),
    [
        # The bug-report case: the single leading slash is stripped, or
        # azurerm builds `.../tfstates//site-monitor-ilo_dev.tfstate`.
        ("/site-monitor-ilo_dev.tfstate", "site-monitor-ilo_dev.tfstate"),
        ("/nested/path/app.tfstate", "nested/path/app.tfstate"),
    ],
)
def test_resolve_backend_config_strips_leading_slash(
    config: Config, tfvars: pathlib.Path, state_key: str, expected: str
) -> None:
    b = Banner.model_validate({"header": "terraform", "project": "demo", "state_key": state_key})
    out = banner.resolve_backend_config(b, tfvars=tfvars, config=config)
    assert out["key"] == expected


@pytest.mark.parametrize(
    "state_key",
    [
        "site-monitor.tfstate",  # not absolute
        "relative/app.tfstate",  # not absolute
        "/nested/../app.tfstate",  # `..` segment
        "/./app.tfstate",  # `.` segment
        "/doubled//slashes.tfstate",  # redundant `//`
        "//leading-double.tfstate",  # leading `//`
        "/trailing/slash/",  # trailing `/`
        "\\windows\\style.tfstate",  # backslashes
        "/",  # empty after stripping
        "///",  # empty after stripping
    ],
)
def test_resolve_backend_config_rejects_impure_state_key(config: Config, tfvars: pathlib.Path, state_key: str) -> None:
    """tf-project is opinionated: non-absolute / un-normalised keys must raise."""
    b = Banner.model_validate({"header": "terraform", "project": "demo", "state_key": state_key})
    with pytest.raises(banner.BannerError, match="state_key"):
        banner.resolve_backend_config(b, tfvars=tfvars, config=config)


def test_render_summary(config: Config, tfvars: pathlib.Path) -> None:
    b = Banner.model_validate(
        {
            "header": "terraform",
            "project": "demo",
            "env": {"A": "1"},
            "backend_config": {"resource_group_name": "rg"},
        }
    )
    summary = banner.render_summary(b, tfvars=tfvars, config=config)
    assert summary["project"] == "demo"
    assert summary["env"] == {"A": "1"}
    assert summary["slot"] == "dev"
    assert summary["slot_is_explicit"] is False
    assert summary["backend_config"]["resource_group_name"] == "rg"
    assert summary["backend_config"]["key"] == "terraform/azure/dev.tfstate"


def test_render_summary_slot_explicit(config: Config, tfvars: pathlib.Path) -> None:
    b = Banner.model_validate({"header": "terraform", "project": "demo", "slot": "custom"})
    summary = banner.render_summary(b, tfvars=tfvars, config=config)
    assert summary["slot"] == "custom"
    assert summary["slot_is_explicit"] is True
