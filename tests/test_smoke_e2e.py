"""End-to-end integration test against a real `terraform` binary.

Uses a `null_resource` so no cloud creds are required. The provider is
still downloaded by `terraform init`, so this test does need network
access (and is gated by the `integration` marker).
"""

from __future__ import annotations

import dataclasses
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
def e2e_config(config: Config) -> Config:
    """Local-backend config: empty state_key_prefix → no -backend-config flags."""
    return dataclasses.replace(config, state_key_prefix="")


def test_init_plan_apply_destroy(e2e_config: Config, tfvars: pathlib.Path) -> None:
    project_dir = e2e_config.terraform_dir / "demo"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "main.tf").write_text(MAIN_TF)
    tfvars.write_text('# {"header":"terraform","project":"demo"}\nvalue = "hello"\n')

    commands.do_init(e2e_config, tfvars=tfvars)
    commands.do_plan(e2e_config)
    assert e2e_config.tfplan_file.exists()

    commands.do_apply(e2e_config)
    # tfplan + sidecar are consumed on success
    assert not e2e_config.tfplan_file.exists()
    assert not pathlib.Path(e2e_config.tfplan_file.as_posix() + commands.TFPLAN_META_SUFFIX).exists()
    # Local backend wrote a terraform.tfstate next to main.tf
    assert (project_dir / "terraform.tfstate").exists()

    commands.do_destroy(e2e_config, extra=["-auto-approve"])
    # After destroy, the saved state contains no resources.
    tfstate_text = (project_dir / "terraform.tfstate").read_text()
    assert '"resources": []' in tfstate_text.replace(" ", "").replace(
        "\n", ""
    ) or '"resources":[]' in tfstate_text.replace(" ", "").replace("\n", "")
