from __future__ import annotations

import json
import pathlib

import pytest

from tf_project import slots
from tf_project.config import Config
from tf_project.state import write_active


def _write_tfvars(root: pathlib.Path, name: str, info: dict[str, object]) -> pathlib.Path:
    path = root / "tfvars" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {json.dumps(info)}\nfoo = \"bar\"\n")
    return path


def test_list_slots_defaults_to_filename_stem(config: Config) -> None:
    out = slots.list_slots(config)
    assert [s.name for s in out] == ["dev"]
    assert out[0].project == "demo"
    assert out[0].name_is_explicit is False


def test_list_slots_uses_explicit_slot_field(config: Config, project_tree: pathlib.Path) -> None:
    _write_tfvars(project_tree, "prod.tfvars", {"header": "terraform", "project": "demo", "slot": "production"})
    names = {s.name for s in slots.list_slots(config)}
    assert names == {"dev", "production"}


def test_validate_unique_passes_for_unique(config: Config) -> None:
    slots.validate_unique(slots.list_slots(config))


def test_validate_unique_raises_on_conflict(config: Config, project_tree: pathlib.Path) -> None:
    # Two tfvars files both default to slot "shared" via their stems.
    _write_tfvars(project_tree, "shared.tfvars", {"header": "terraform", "project": "demo"})
    _write_tfvars(project_tree, "other.tfvars", {"header": "terraform", "project": "demo", "slot": "shared"})
    with pytest.raises(slots.SlotConflictError) as exc:
        slots.validate_unique(slots.list_slots(config))
    msg = str(exc.value)
    assert "shared" in msg
    assert "explicit" in msg and "default" in msg


def test_resolve_slot_by_name(config: Config) -> None:
    s = slots.resolve_slot("dev", config=config)
    assert s.name == "dev"
    assert s.project == "demo"
    assert s.data_dir == s.slot_dir / ".terraform"


def test_resolve_slot_by_path(config: Config, tfvars: pathlib.Path) -> None:
    s = slots.resolve_slot(str(tfvars), config=config)
    assert s.name == "dev"


def test_resolve_slot_unknown_name(config: Config) -> None:
    with pytest.raises(slots.SlotError):
        slots.resolve_slot("ghost", config=config)


def test_resolve_active_priority_flag_over_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFP_SLOT", "from-env")
    write_active(config, "from-file")
    a = slots.resolve_active(config, flag="from-flag")
    assert a is not None and a.name == "from-flag" and a.source == "flag"


def test_resolve_active_env_over_file(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TFP_SLOT", "from-env")
    write_active(config, "from-file")
    a = slots.resolve_active(config, flag=None)
    assert a is not None and a.name == "from-env" and a.source == "env"


def test_resolve_active_file_fallback(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TFP_SLOT", raising=False)
    write_active(config, "from-file")
    a = slots.resolve_active(config, flag=None)
    assert a is not None and a.name == "from-file" and a.source == "file"


def test_resolve_active_returns_none_when_unset(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TFP_SLOT", raising=False)
    assert slots.resolve_active(config, flag=None) is None


def test_assert_slot_exists_passes_for_known(config: Config) -> None:
    slots.assert_slot_exists(config, "dev")


def test_assert_slot_exists_raises_for_unknown(config: Config) -> None:
    with pytest.raises(slots.SlotError):
        slots.assert_slot_exists(config, "ghost")


def test_list_orphans(config: Config) -> None:
    # Create a slot dir with no matching banner.
    ghost = config.slot_dir_for("ghost")
    ghost.mkdir(parents=True)
    orphans = slots.list_orphans(config)
    assert len(orphans) == 1
    assert orphans[0].name == "ghost"
    assert orphans[0].slot_dir == ghost.resolve()
