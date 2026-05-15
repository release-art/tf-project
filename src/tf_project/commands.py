"""Top-level command implementations, decoupled from the CLI layer."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
from collections.abc import Sequence

from tf_project import banner, terraform
from tf_project.config import Config
from tf_project.secrets import SecretsProvider, provider_from_config
from tf_project.state import MyState, try_exclusive_lock

# Subcommands implemented natively below; everything else falls through to
# `terraform` directly via `do_passthrough`.
WRAPPED_SUBCOMMANDS: frozenset[str] = frozenset(
    {"init", "plan", "apply", "refresh", "destroy", "fmt", "output", "state-mv", "status", "last"}
)

TFPLAN_META_SUFFIX = ".meta.json"


class StateNotInitializedError(RuntimeError):
    """Raised when a command needs a saved state but `init` hasn't been run."""


class StaleTfplanError(RuntimeError):
    """The saved tfplan was generated against a different tfvars content."""


def _apply_lock_path(config: Config) -> pathlib.Path:
    return config.tmp_dir / "apply.lock"


def _require_state(config: Config) -> MyState:
    state = MyState.load(config)
    if state is None:
        raise StateNotInitializedError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return state


def _provider(config: Config) -> SecretsProvider:
    return provider_from_config(config.secrets)


def _extras(extra: Sequence[str] | None) -> list[str]:
    return list(extra) if extra else []


def _plan_inputs_sha256(state: MyState, decrypted: pathlib.Path) -> str:
    """Rolling hash over the decrypted tfvars plus every `.tf` under source_root.

    Captures both kinds of drift between plan and apply: a changed tfvars
    (e.g. rotated secrets) and changed module code. Files are visited in
    sorted order with their path mixed in, so renames also invalidate the
    plan.
    """
    h = hashlib.sha256()
    h.update(b"tfvars:")
    h.update(decrypted.read_bytes())
    source_root = pathlib.Path(state.source_root)
    if source_root.is_dir():
        for tf_file in sorted(source_root.rglob("*.tf")):
            rel = tf_file.relative_to(source_root)
            h.update(b"\x00tf:")
            h.update(str(rel).encode("utf-8"))
            h.update(b"\x00")
            h.update(tf_file.read_bytes())
    return h.hexdigest()


def _tfplan_meta_path(state: MyState) -> pathlib.Path:
    return pathlib.Path(state.tfplan_location + TFPLAN_META_SUFFIX)


def _write_tfplan_meta(state: MyState, *, decrypted: pathlib.Path) -> None:
    meta = {"inputs_sha256": _plan_inputs_sha256(state, decrypted)}
    _tfplan_meta_path(state).write_text(json.dumps(meta, sort_keys=True))


def _check_tfplan_fresh(state: MyState, *, decrypted: pathlib.Path) -> None:
    meta_path = _tfplan_meta_path(state)
    if not meta_path.exists():
        return
    payload = json.loads(meta_path.read_text())
    # Accept the legacy field name from earlier versions for forward-compat.
    expected = payload.get("inputs_sha256") or payload.get("tfvars_sha256")
    actual = _plan_inputs_sha256(state, decrypted)
    if expected and expected != actual:
        raise StaleTfplanError(
            f"Saved tfplan at {state.tfplan_location} was generated against different "
            f"inputs (sha256 {expected[:12]}…) than the current tfvars + .tf source "
            f"(sha256 {actual[:12]}…). Re-run `tfp plan` or use `tfp apply --force`."
        )


def build_init_argv(config: Config, *, state: MyState, extra: Sequence[str] | None = None) -> list[str]:
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
    return cmd


def build_plan_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    replaces: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "plan",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *terraform.replace_args(replaces),
        *_extras(extra),
        f"-out={state.tfplan_location}",
    ]


def build_apply_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "apply",
        f"-var-file={var_file}",
        *_extras(extra),
        state.tfplan_location,
    ]


def build_refresh_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "apply",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *_extras(extra),
    ]


def build_destroy_argv(
    config: Config,
    *,
    state: MyState,
    var_file: str,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "destroy",
        f"-var-file={var_file}",
        *terraform.target_args(targets),
        *_extras(extra),
    ]


def build_output_argv(config: Config, *, state: MyState, extra: Sequence[str] | None = None) -> list[str]:
    return [
        config.terraform_binary,
        f"-chdir={state.source_root}",
        "output",
        "-json",
        *_extras(extra),
    ]


def do_init(config: Config, *, tfvars: pathlib.Path, extra: Sequence[str] | None = None) -> None:
    tfvars = tfvars.resolve()
    project_info = banner.find_project_info(tfvars)
    project_name = banner.parse_project(project_info, tfvars=tfvars)
    print(f"==== PROJECT: {project_name} ====")

    backend_config = banner.resolve_backend_config(project_info, tfvars=tfvars, config=config)
    banner_env = banner.parse_env(project_info, tfvars=tfvars)

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
        backend_config=backend_config,
    )

    terraform.run(build_init_argv(config, state=state, extra=extra))
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
            build_plan_argv(
                config,
                state=state,
                var_file=str(decrypted),
                targets=targets,
                replaces=replaces,
                extra=extra,
            ),
            env=terraform.merged_env(state.environ),
        )
        _write_tfplan_meta(state, decrypted=decrypted)


def do_apply(config: Config, *, force: bool = False, extra: Sequence[str] | None = None) -> None:
    state = _require_state(config)
    with try_exclusive_lock(_apply_lock_path(config)):
        with state.decrypted_tfvars(_provider(config)) as decrypted:
            if not force:
                _check_tfplan_fresh(state, decrypted=decrypted)
            terraform.run(
                build_apply_argv(config, state=state, var_file=str(decrypted), extra=extra),
                env=terraform.merged_env(state.environ),
            )
        pathlib.Path(state.tfplan_location).unlink(missing_ok=True)
        _tfplan_meta_path(state).unlink(missing_ok=True)


def do_refresh(
    config: Config,
    *,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    with try_exclusive_lock(_apply_lock_path(config)):
        with state.decrypted_tfvars(_provider(config)) as decrypted:
            terraform.run(
                build_refresh_argv(
                    config,
                    state=state,
                    var_file=str(decrypted),
                    targets=targets,
                    extra=extra,
                ),
                env=terraform.merged_env(state.environ),
            )


def do_destroy(
    config: Config,
    *,
    targets: Sequence[str] | None = None,
    extra: Sequence[str] | None = None,
) -> None:
    state = _require_state(config)
    with try_exclusive_lock(_apply_lock_path(config)):
        with state.decrypted_tfvars(_provider(config)) as decrypted:
            terraform.run(
                build_destroy_argv(
                    config,
                    state=state,
                    var_file=str(decrypted),
                    targets=targets,
                    extra=extra,
                ),
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
    terraform.run(build_output_argv(config, state=state, extra=extra))


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
    """Forward an arbitrary terraform invocation by replacing this process.

    Uses `os.execvpe` so signals and exit code are fully native — no Python
    in the loop. Returns only in dry-run mode (or in tests, where
    `exec_passthrough` is mocked).
    """
    state = MyState.load(config)
    cmd: list[str] = [config.terraform_binary]
    if state is not None:
        cmd.append(f"-chdir={state.source_root}")
    cmd.extend(args)
    env = terraform.merged_env(state.environ) if state is not None else None
    terraform.exec_passthrough(cmd, env=env)


@dataclasses.dataclass(frozen=True, slots=True)
class StatusReport:
    initialized: bool
    tfvars: str | None
    source_root: str | None
    backend_key: str | None
    env_keys: tuple[str, ...]
    plan_ready: bool


def status_report(config: Config) -> StatusReport:
    state = MyState.load(config)
    if state is None:
        return StatusReport(
            initialized=False,
            tfvars=None,
            source_root=None,
            backend_key=None,
            env_keys=(),
            plan_ready=False,
        )
    return StatusReport(
        initialized=True,
        tfvars=state.tfvars,
        source_root=state.source_root,
        backend_key=state.backend_config.get("key"),
        env_keys=tuple(sorted(state.environ)),
        plan_ready=pathlib.Path(state.tfplan_location).exists(),
    )
