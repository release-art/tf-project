from __future__ import annotations

import json
import pathlib

from tf_project import migrate
from tf_project.config import Config
from tf_project.state import load_state, read_active


def _write_legacy(config: Config, tfvars: pathlib.Path) -> None:
    legacy = {
        "tfvars": str(tfvars),
        "source_root": str(config.terraform_dir / "demo"),
        "tfplan_location": str(config.tmp_dir / "my.tfplan"),
        "environ": {"X": "1"},
        "backend_config": {"key": "shared/dev.tfstate"},
    }
    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(json.dumps(legacy))


def test_no_legacy_state_is_noop(config: Config) -> None:
    result = migrate.run_if_needed(config)
    assert result.migrated is False


def test_legacy_state_promoted_to_slot(config: Config, tfvars: pathlib.Path) -> None:
    _write_legacy(config, tfvars)
    result = migrate.run_if_needed(config)
    assert result.migrated is True
    assert result.slot == "dev"
    assert read_active(config) == "dev"
    state = load_state(config, "dev")
    assert state is not None
    assert state.slot == "dev"
    assert state.environ == {"X": "1"}
    assert not (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).exists()
    # Breadcrumb is written.
    assert result.breadcrumb is not None and result.breadcrumb.exists()


def test_legacy_state_idempotent_after_migration(config: Config, tfvars: pathlib.Path) -> None:
    _write_legacy(config, tfvars)
    migrate.run_if_needed(config)
    second = migrate.run_if_needed(config)
    assert second.migrated is False


def test_legacy_state_missing_tfvars(config: Config) -> None:
    legacy = {
        "tfvars": str(config.project_root / "missing.tfvars"),
        "source_root": "/tmp/src",
        "tfplan_location": "/tmp/x.tfplan",
        "environ": {},
        "backend_config": {},
    }
    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(json.dumps(legacy))
    result = migrate.run_if_needed(config)
    assert result.migrated is False
    assert result.note is not None and "missing tfvars" in result.note
    # Legacy file removed so we don't loop forever.
    assert not (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).exists()


def test_legacy_state_unreadable(config: Config) -> None:
    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text("not json at all")
    result = migrate.run_if_needed(config)
    assert result.migrated is False
    assert not (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).exists()
