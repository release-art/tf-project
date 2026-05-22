from __future__ import annotations

import json
import pathlib
import textwrap
from collections.abc import Callable

import pytest

from tf_project.config import Config, SecretsConfig
from tf_project.models import MyState
from tf_project.state import save_state, write_active

# ---- Fixtures: project layout ------------------------------------------------


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
    root = project_tree.resolve()
    return Config(
        project_root=root,
        terraform_dir=(root / "terraform").resolve(),
        tfvars_dir=(root / "tfvars").resolve(),
        tmp_dir=(root / "tmp").resolve(),
        state_key_prefix="terraform/azure/",
        terraform_binary="terraform",
        backend_config={},
        secrets=SecretsConfig(command=()),  # noop — don't shell out to `op` in tests
    )


@pytest.fixture
def tfvars(project_tree: pathlib.Path) -> pathlib.Path:
    return (project_tree / "tfvars" / "dev.tfvars").resolve()


# ---- Fixtures: state setup ---------------------------------------------------


@pytest.fixture
def save_dev_state(config: Config, tfvars: pathlib.Path) -> Callable[..., MyState]:
    """Factory that seeds a `dev`-slot MyState + active pointer.

    Returns a callable so individual tests can override fields. Default
    backend is the synthetic "key=terraform/azure/dev.tfstate" used by
    `config`'s `state_key_prefix`.
    """

    def _make(**overrides: object) -> MyState:
        defaults = {
            "slot": "dev",
            "project": "demo",
            "tfvars": tfvars,
            "source_root": (config.terraform_dir / "demo").resolve(),
            "tfplan_location": config.tfplan_file_for("dev"),
            "environ": {"ARM_SUBSCRIPTION_ID": "abc"},
            "backend_config": {"key": "terraform/azure/dev.tfstate"},
        }
        defaults.update(overrides)
        state = MyState(**defaults)  # type: ignore[arg-type]
        save_state(state, config)
        write_active(config, state.slot)
        return state

    return _make


# ---- Fixtures: terraform.run capture -----------------------------------------


@pytest.fixture
def run_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Capture every terraform.run / exec_passthrough call without exec'ing.

    Tests assert on the captured argv + env. Replaces both run and
    exec_passthrough so passthrough-tests don't replace this process.
    """
    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
        calls.append({"cmd": cmd, "env": env})

    monkeypatch.setattr("tf_project.commands.terraform.run", fake_run)
    monkeypatch.setattr("tf_project.commands.terraform.exec_passthrough", fake_run)
    return calls


# ---- Helpers: config-file authoring ------------------------------------------


@pytest.fixture
def write_tfp_toml() -> Callable[..., None]:
    """Factory: drop a tf_project.toml at `root` with the default layout.

    `state_key_prefix` is the only field tests vary in practice; everything
    else hard-codes `terraform_dir="terraform"`, `tfvars_dir="tfvars"`,
    `tmp_dir="tmp"`, and empty secrets. Pass `body` to supply a complete
    file instead (for the few tests exercising config parsing itself).
    """

    def _write(root: pathlib.Path, *, state_key_prefix: str = "", body: str | None = None) -> None:
        if body is not None:
            (root / "tf_project.toml").write_text(textwrap.dedent(body))
            return
        (root / "tf_project.toml").write_text(
            "[tf_project]\n"
            'terraform_dir = "terraform"\n'
            'tfvars_dir = "tfvars"\n'
            'tmp_dir = "tmp"\n'
            f'state_key_prefix = "{state_key_prefix}"\n\n'
            "[tf_project.secrets]\n"
            "command = []\n"
        )

    return _write
