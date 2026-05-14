from __future__ import annotations

import shutil

import pytest

from tf_project import commands
from tf_project.config import Config

pytestmark = pytest.mark.integration

UNFORMATTED = 'resource    "null_resource"   "x"     {}\n'
FORMATTED = 'resource "null_resource" "x" {}\n'


@pytest.fixture(autouse=True)
def _skip_if_terraform_missing() -> None:
    if shutil.which("terraform") is None:
        pytest.skip("terraform binary not on PATH")


def test_fmt_formats_in_place(config: Config) -> None:
    tf_file = config.terraform_dir / "demo" / "main.tf"
    tf_file.parent.mkdir(parents=True, exist_ok=True)
    tf_file.write_text(UNFORMATTED)
    commands.do_fmt(config)
    assert tf_file.read_text() == FORMATTED


def test_fmt_idempotent(config: Config) -> None:
    tf_file = config.terraform_dir / "demo" / "main.tf"
    tf_file.parent.mkdir(parents=True, exist_ok=True)
    tf_file.write_text(FORMATTED)
    commands.do_fmt(config)
    commands.do_fmt(config)
    assert tf_file.read_text() == FORMATTED
