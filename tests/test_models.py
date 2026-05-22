from __future__ import annotations

import json
import pathlib

import pytest

from tf_project.models import (
    MyState,
    SchemaError,
    SchemaIncompatibleError,
    TfplanMeta,
    VersionedModel,
)


def _payload(**overrides: object) -> dict[str, object]:
    base = {
        "schema_version": 1,
        "slot": "dev",
        "tfvars": "/tmp/dev.tfvars",
        "source_root": "/tmp/src",
        "tfplan_location": "/tmp/dev/tfplan",
        "environ": {"X": "1"},
        "backend_config": {"key": "k"},
    }
    base.update(overrides)
    return base


def test_my_state_round_trip(tmp_path: pathlib.Path) -> None:
    s = MyState.model_validate(_payload())
    path = tmp_path / "state.json"
    s.dump_file(path)
    loaded = MyState.load_file(path)
    assert loaded == s


def test_my_state_missing_schema_version_rejected(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "state.json"
    payload = _payload()
    payload.pop("schema_version")
    p.write_text(json.dumps(payload))
    with pytest.raises(SchemaError):
        MyState.load_file(p)


def test_my_state_extra_field_rejected(tmp_path: pathlib.Path) -> None:
    from pydantic import ValidationError

    p = tmp_path / "state.json"
    p.write_text(json.dumps(_payload(unknown="oops")))
    with pytest.raises(ValidationError):
        MyState.load_file(p)


def test_my_state_future_version_rejected(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "state.json"
    p.write_text(json.dumps(_payload(schema_version=999)))
    with pytest.raises(SchemaIncompatibleError):
        MyState.load_file(p)


def test_my_state_top_level_not_an_object(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("[]")
    with pytest.raises(SchemaError):
        MyState.load_file(p)


def test_tfplan_meta_round_trip(tmp_path: pathlib.Path) -> None:
    meta = TfplanMeta(
        inputs_sha256="abc" * 21 + "x",
        project="demo",
        state_key="k",
        backend_config={"key": "k"},
        env={"A": "1"},
    )
    p = tmp_path / "meta.json"
    meta.dump_file(p)
    loaded = TfplanMeta.load_file(p)
    assert loaded == meta


def test_versioned_model_class_var_is_required() -> None:
    # Subclasses must declare SUPPORTED_VERSION via ClassVar.
    assert MyState.SUPPORTED_VERSION == 1
    assert TfplanMeta.SUPPORTED_VERSION == 1
    assert issubclass(MyState, VersionedModel)
