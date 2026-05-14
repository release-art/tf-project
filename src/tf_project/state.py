"""Persistent per-init state for the `tf_project` CLI."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import pathlib
from collections.abc import Iterator

from tf_project.config import Config
from tf_project.secrets import SecretsProvider


@dataclasses.dataclass(kw_only=True, slots=True)
class MyState:
    tfvars: str
    source_root: str
    tfplan_location: str
    environ: dict[str, str]
    backend_config: dict[str, str]

    @classmethod
    def load(cls, config: Config) -> "MyState | None":
        if not config.state_file.exists():
            return None
        with config.state_file.open("r") as fin:
            return cls(**json.load(fin))

    def save(self, config: Config) -> None:
        config.state_file.parent.mkdir(parents=True, exist_ok=True)
        with config.state_file.open("w") as fout:
            json.dump(dataclasses.asdict(self), fout, indent=4, sort_keys=True)

    @contextlib.contextmanager
    def decrypted_tfvars(self, provider: SecretsProvider) -> Iterator[pathlib.Path]:
        with provider.materialize(pathlib.Path(self.tfvars)) as path:
            yield path
