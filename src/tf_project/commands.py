"""Top-level command implementations, decoupled from the CLI layer."""

from __future__ import annotations

import pathlib
from collections.abc import Sequence

from tf_project import terraform
from tf_project.config import Config
from tf_project.secrets import SecretsProvider, provider_from_config
from tf_project.state import MyState

# Subcommands implemented natively below; everything else falls through to
# `terraform` directly via `do_passthrough`.
WRAPPED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"init", "plan", "apply", "refresh", "destroy", "fmt", "output", "state-mv"}
)


class StateNotInitializedError(RuntimeError):
    """Raised when a command needs a saved state but `init` hasn't been run."""


def _require_state(config: Config) -> MyState:
    state = MyState.load(config)
    if state is None:
        raise StateNotInitializedError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return state


def _provider(config: Config) -> SecretsProvider:
    return provider_from_config(config.secrets)


def _extras(extra: Sequence[str] | None) -> list[str]:
    return list(extra) if extra else []


def _banner_state_key(banner: dict[str, object], *, tfvars: pathlib.Path, config: Config) -> str:
    """Resolve the remote-state key, honouring an optional `state_key` banner override."""
    override = banner.get("state_key")
    if override is None:
        return f"{config.state_key_prefix}{tfvars.stem}.tfstate"
    if not isinstance(override, str) or not override:
        raise ValueError(f"`state_key` in terraform banner of {tfvars} must be a non-empty string")
    return override


def _banner_env(banner: dict[str, object], *, tfvars: pathlib.Path) -> dict[str, str]:
    """Validate and return the `env` block from a tfvars banner."""
    raw = banner.get("env")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"`env` in terraform banner of {tfvars} must be a JSON object")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"`env` in terraform banner of {tfvars} must map string keys to string values; got {key!r}={value!r}"
            )
        out[key] = value
    return out


def do_init(config: Config, *, tfvars: pathlib.Path, extra: Sequence[str] | None = None) -> None:
    tfvars = tfvars.resolve()
    project_info = terraform.find_project_info(tfvars)
    project_name = project_info.get("project")
    if not isinstance(project_name, str) or not project_name:
        raise ValueError(f"`project` missing or invalid in terraform banner of {tfvars}")
    print(f"==== PROJECT: {project_name} ====")

    state_key = _banner_state_key(project_info, tfvars=tfvars, config=config)
    banner_env = _banner_env(project_info, tfvars=tfvars)

    config.tmp_dir.mkdir(parents=True, exist_ok=True)
    old_state = MyState.load(config)
    init_env: dict[str, str] = {}
    if old_state is not None:
        init_env.update(old_state.environ)
    init_env.update(banner_env)  # banner wins over previously-saved state env

    state = MyState(
        tfvars=str(tfvars),
        source_root=str(config.terraform_dir / project_name),
        tfplan_location=str(config.tfplan_file),
        environ=init_env,
        backend_config={"key": state_key},
    )

    cmd = [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "init",
        "-upgrade",
        "-reconfigure",
        *_extras(extra),
    ]
    for key, value in state.backend_config.items():
        cmd.extend(["-backend-config", f"{key}={value}"])
    terraform.run(cmd)
    state.save(config)


def do_plan(
    config: Config,
    *,
    targets: Sequence[str] | None = None,
    replaces: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    with state.decrypted_tfvars(_provider(config)) as decrypted:
        terraform.run(
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "plan",
                f"-var-file={decrypted}",
                *terraform.target_args(targets),
                *terraform.replace_args(replaces),
                *_extras(extra),
                f"-out={state.tfplan_location}",
            ],
            env=terraform.merged_env(state.environ),
        )


def do_apply(config: Config, *, extra: Sequence[str] | None = None) -> None:
    state = _require_state(config)
    with state.decrypted_tfvars(_provider(config)) as decrypted:
        terraform.run(
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "apply",
                f"-var-file={decrypted}",
                *_extras(extra),
                state.tfplan_location,
            ],
            env=terraform.merged_env(state.environ),
        )
    pathlib.Path(state.tfplan_location).unlink(missing_ok=True)


def do_refresh(
    config: Config,
    *,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    with state.decrypted_tfvars(_provider(config)) as decrypted:
        terraform.run(
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "apply",
                f"-var-file={decrypted}",
                *terraform.target_args(targets),
                *_extras(extra),
            ],
            env=terraform.merged_env(state.environ),
        )


def do_destroy(
    config: Config,
    *,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    with state.decrypted_tfvars(_provider(config)) as decrypted:
        terraform.run(
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "destroy",
                f"-var-file={decrypted}",
                *terraform.target_args(targets),
                *_extras(extra),
            ],
            env=terraform.merged_env(state.environ),
        )


def do_fmt(config: Config, *, extra: Sequence[str] | None = None) -> None:
    paths: list[str] = []
    if config.terraform_dir.exists():
        paths.append(str(config.terraform_dir))
    if config.tfvars_dir.exists():
        paths.append(str(config.tfvars_dir))
    if not paths:
        raise FileNotFoundError(f"Neither {config.terraform_dir} nor {config.tfvars_dir} exists to format.")
    terraform.run([config.terraform_binary, "fmt", "-recursive", *_extras(extra), *paths])


def do_output(config: Config, *, extra: Sequence[str] | None = None) -> None:
    state = _require_state(config)
    terraform.run(
        [
            config.terraform_binary,
            f"-chdir={state.source_root}",
            "output",
            "-json",
            *_extras(extra),
        ]
    )


def do_state_mv(
    config: Config,
    *,
    source: str,
    destination: str,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    terraform.run(
        [
            config.terraform_binary,
            f"-chdir={state.source_root}",
            "state",
            "mv",
            *_extras(extra),
            source,
            destination,
        ],
        env=terraform.merged_env(state.environ),
    )


def do_passthrough(config: Config, args: Sequence[str]) -> None:
    """Forward an arbitrary terraform invocation, preserving `-chdir` and env from state."""
    state = MyState.load(config)
    cmd: list[str] = [config.terraform_binary]
    if state is not None:
        cmd.append(f"-chdir={state.source_root}")
    cmd.extend(args)
    env = terraform.merged_env(state.environ) if state is not None else None
    terraform.run(cmd, env=env)
