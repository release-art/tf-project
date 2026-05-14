from __future__ import annotations

import json
import pathlib

import pytest

from tf_project.config import Config, SecretsConfig


@pytest.fixture
def project_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """Lay out a fake consumer project rooted at tmp_path."""
    (tmp_path / "terraform" / "demo").mkdir(parents=True)
    (tmp_path / "tfvars").mkdir()
    (tmp_path / "tmp").mkdir()
    banner = {"header": "terraform", "project": "demo"}
    tfvars = tmp_path / "tfvars" / "dev.tfvars"
    tfvars.write_text(f'# {json.dumps(banner)}\nfoo = "bar"\n')
    return tmp_path


@pytest.fixture
def config(project_tree: pathlib.Path) -> Config:
    root = project_tree
    return Config(
        project_root=root,
        terraform_dir=(root / "terraform").resolve(),
        tfvars_dir=(root / "tfvars").resolve(),
        tmp_dir=(root / "tmp").resolve(),
        state_key_prefix="terraform/azure/",
        state_file=(root / "tmp" / "my_terraform_state.json").resolve(),
        tfplan_file=(root / "tmp" / "my.tfplan").resolve(),
        terraform_binary="terraform",
        secrets=SecretsConfig(command=()),  # noop — don't shell out to `op` in tests
    )


@pytest.fixture
def tfvars(project_tree: pathlib.Path) -> pathlib.Path:
    return (project_tree / "tfvars" / "dev.tfvars").resolve()
