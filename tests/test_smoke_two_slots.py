"""Integration test: switching between two warm slots does not re-init.

Verifies the core ergonomics promise of the slot model — once a slot's
`.terraform/` is populated, `tfp use <slot>` flips the pointer and does
not spawn `terraform init`.
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

from tf_project import commands
from tf_project.config import Config

pytestmark = pytest.mark.integration

MAIN_TF = """\
terraform {
  required_providers {
    null = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

variable "value" { type = string }

resource "null_resource" "x" {
  triggers = { v = var.value }
}
"""


@pytest.fixture(autouse=True)
def _skip_if_terraform_missing() -> None:
    if shutil.which("terraform") is None:
        pytest.skip("terraform binary not on PATH")


@pytest.fixture
def two_slot_config(config: Config, project_tree: pathlib.Path) -> Config:
    """Set up two tfvars pointing at the same project, local backend."""
    project_dir = config.terraform_dir / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "main.tf").write_text(MAIN_TF)
    # `dev` (already created by the `project_tree` fixture) and `prod`.
    (config.tfvars_dir / "dev.tfvars").write_text(
        '# {"header":"terraform","project":"demo"}\nvalue = "from-dev"\n'
    )
    (config.tfvars_dir / "prod.tfvars").write_text(
        '# {"header":"terraform","project":"demo"}\nvalue = "from-prod"\n'
    )
    return config.model_copy(update={"state_key_prefix": ""})


def test_warm_slot_switch_does_not_init(two_slot_config: Config) -> None:
    cfg = two_slot_config

    commands.do_use(cfg, arg="dev")
    assert cfg.data_dir_for("dev").is_dir()

    commands.do_use(cfg, arg="prod")
    assert cfg.data_dir_for("prod").is_dir()

    # Both slots have their own `.terraform/providers/` populated by init.
    assert (cfg.data_dir_for("dev") / "providers").is_dir()
    assert (cfg.data_dir_for("prod") / "providers").is_dir()

    # Switching back to the warm slot should not call terraform init.
    # We detect this via the backend.hash file's mtime — a successful
    # init bumps it, a warm switch doesn't.
    mtime_before = cfg.backend_hash_for("dev").stat().st_mtime
    commands.do_use(cfg, arg="dev")
    mtime_after = cfg.backend_hash_for("dev").stat().st_mtime
    assert mtime_after == mtime_before, "warm switch unexpectedly re-initialised the slot"


def test_plugin_cache_populated(two_slot_config: Config) -> None:
    cfg = two_slot_config
    commands.do_use(cfg, arg="dev")
    # Plugin cache should contain at least one provider directory after init.
    cache = cfg.effective_plugin_cache_dir
    assert cache.is_dir()
    # `registry.terraform.io/hashicorp/null/<version>/<platform>/`
    matches = list(cache.rglob("terraform-provider-null*"))
    assert matches, f"expected null provider in plugin cache, found nothing under {cache}"
