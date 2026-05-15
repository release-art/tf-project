"""Implementations behind `tfp self ...` subcommands."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import tomllib
from typing import Any, NamedTuple

from tf_project import banner, lock
from tf_project.config import (
    CONFIG_FILE_NAME,
    CONFIG_TABLE,
    DEFAULT_SECRETS_COMMAND,
    DEFAULT_TERRAFORM_DIR,
    DEFAULT_TFVARS_DIR,
    DEFAULT_TMP_DIR,
    PYPROJECT_FILE_NAME,
    PYPROJECT_TABLE,
    Config,
    ConfigNotFoundError,
)
from tf_project.state import MyState


class SelfCommandError(RuntimeError):
    """User-visible failure inside a `self` subcommand."""


@dataclasses.dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


_TABLE_PREFIX_PLACEHOLDER = "__TF_PROJECT_TABLE_PREFIX__"

DEFAULT_CONFIG_BODY = f"""\
terraform_dir = "{DEFAULT_TERRAFORM_DIR}"
tfvars_dir    = "{DEFAULT_TFVARS_DIR}"
tmp_dir       = "{DEFAULT_TMP_DIR}"
state_key_prefix = ""
# terraform_binary = "terraform"       # path or PATH-name; defaults to `which terraform`

[{_TABLE_PREFIX_PLACEHOLDER}.secrets]
# Pluggable tfvars preprocessor. Set `command = []` to disable.
command = {list(DEFAULT_SECRETS_COMMAND)!r}
"""


def _render_default_body(*, prefix: str) -> str:
    """Render the default config snippet with the right table-prefix.

    `prefix` is either ``"tf_project"`` (standalone file) or
    ``"tool.tf_project"`` (pyproject.toml).
    """
    header = f"[{prefix}]\n"
    return header + DEFAULT_CONFIG_BODY.replace(_TABLE_PREFIX_PLACEHOLDER, prefix)


def do_self_init(cwd: pathlib.Path) -> pathlib.Path:
    """Bootstrap a config in `cwd`. Refuses to overwrite an existing config.

    Returns the path of the file that was created or modified.
    """
    pyproject = cwd / PYPROJECT_FILE_NAME
    standalone = cwd / CONFIG_FILE_NAME

    if standalone.exists():
        raise SelfCommandError(f"{standalone} already exists; not overwriting.")

    if pyproject.exists():
        with pyproject.open("rb") as fin:
            data = tomllib.load(fin)
        if _has_table(data, PYPROJECT_TABLE):
            raise SelfCommandError(f"[{'.'.join(PYPROJECT_TABLE)}] already exists in {pyproject}; not overwriting.")
        snippet = _render_default_body(prefix=".".join(PYPROJECT_TABLE))
        existing = pyproject.read_text()
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        pyproject.write_text(existing + sep + snippet)
        return pyproject

    standalone.write_text(_render_default_body(prefix=CONFIG_TABLE))
    return standalone


def do_self_config_print(config: Config, *, as_json: bool) -> str:
    payload = _config_to_dict(config)
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True)
    lines = []
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k} = {v}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines)


def do_self_config_path(cwd: pathlib.Path) -> pathlib.Path:
    """Return the file Config.discover would pick up from `cwd`."""
    cwd = cwd.resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / CONFIG_FILE_NAME
        if candidate.is_file():
            return candidate
    for parent in [cwd, *cwd.parents]:
        candidate = parent / PYPROJECT_FILE_NAME
        if not candidate.is_file():
            continue
        with candidate.open("rb") as fin:
            data = tomllib.load(fin)
        if _has_table(data, PYPROJECT_TABLE):
            return candidate
    raise ConfigNotFoundError(
        f"No tf_project config found by walking up from {cwd}. Run `tfp self init` to create one."
    )


def do_self_state_show(config: Config) -> str:
    state = MyState.load(config)
    if state is None:
        raise SelfCommandError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return json.dumps(dataclasses.asdict(state), indent=2, sort_keys=True)


def do_self_state_clear(config: Config) -> bool:
    """Remove the saved state file. Returns True if a file was removed."""
    if not config.state_file.exists():
        return False
    config.state_file.unlink()
    return True


def do_self_doctor(config: Config) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []

    checks.append(_terraform_binary_check(config.terraform_binary))

    if config.secrets.command:
        secrets_bin = config.secrets.command[0]
        resolved = shutil.which(secrets_bin)
        checks.append(
            DoctorCheck(
                name=f"secrets command `{secrets_bin}` on PATH",
                ok=resolved is not None,
                detail=resolved or f"not found; install `{secrets_bin}` or disable in [tf_project.secrets]",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="secrets command",
                ok=True,
                detail="disabled (command = [])",
            )
        )

    checks.append(_dir_check("terraform_dir", config.terraform_dir))
    checks.append(_dir_check("tfvars_dir", config.tfvars_dir))
    checks.append(_dir_check("tmp_dir", config.tmp_dir, allow_missing=True))

    return checks


def _terraform_binary_check(binary: str) -> DoctorCheck:
    name = f"terraform binary `{binary}`"
    if "/" in binary or "\\" in binary:
        path = pathlib.Path(binary)
        if path.is_file():
            return DoctorCheck(name=name, ok=True, detail=str(path))
        return DoctorCheck(name=name, ok=False, detail=f"{path} (not a file)")
    resolved = shutil.which(binary)
    if resolved is not None:
        return DoctorCheck(name=name, ok=True, detail=resolved)
    return DoctorCheck(name=name, ok=False, detail="not found on PATH; install or set `terraform_binary`")


def _dir_check(name: str, path: pathlib.Path, *, allow_missing: bool = False) -> DoctorCheck:
    if path.is_dir():
        return DoctorCheck(name=f"{name} exists", ok=True, detail=str(path))
    if allow_missing:
        return DoctorCheck(
            name=f"{name} exists",
            ok=True,
            detail=f"{path} (missing — will be created on demand)",
        )
    return DoctorCheck(name=f"{name} exists", ok=False, detail=str(path))


def _has_table(data: dict[str, Any], keys: tuple[str, ...]) -> bool:
    cursor: Any = data
    for key in keys:
        if not isinstance(cursor, dict) or key not in cursor:
            return False
        cursor = cursor[key]
    return isinstance(cursor, dict)


def _config_to_dict(config: Config) -> dict[str, Any]:
    return {
        "project_root": str(config.project_root),
        "terraform_dir": str(config.terraform_dir),
        "tfvars_dir": str(config.tfvars_dir),
        "tmp_dir": str(config.tmp_dir),
        "state_key_prefix": config.state_key_prefix,
        "state_file": str(config.state_file),
        "tfplan_file": str(config.tfplan_file),
        "terraform_binary": config.terraform_binary,
        "backend_config": dict(config.backend_config),
        "secrets": {"command": list(config.secrets.command)},
    }


def do_self_banner_check(config: Config, *, tfvars: pathlib.Path) -> dict[str, Any]:
    """Parse and validate the banner in `tfvars`; return the resolved summary."""
    info = banner.find_project_info(tfvars.resolve())
    return banner.render_summary(info, tfvars=tfvars.resolve(), config=config)


# ---- `tfp self trace` ---------------------------------------------------------

_TRACE_PLACEHOLDER = "<DECRYPTED_TFVARS>"


def do_self_trace(
    config: Config,
    *,
    subcommand: str,
    targets: list[str] | None = None,
    replaces: list[str] | None = None,
    extra: list[str] | None = None,
    import_address: str = "<RESOURCE_ADDRESS>",
    import_id: str = "<RESOURCE_ID>",
) -> list[str]:
    """Return the argv `tfp <subcommand>` would build, without invoking anything.

    The decrypted-tfvars path is rendered as `<DECRYPTED_TFVARS>` since op
    inject isn't actually run.
    """
    from tf_project import commands  # late import to avoid circular

    state = MyState.load(config)
    if state is None:
        raise SelfCommandError("Not initialized. Run `tfp init <tfvars>` first.")

    builders = {
        "init": lambda: commands.build_init_argv(config, state=state, extra=extra),
        "plan": lambda: commands.build_plan_argv(
            config,
            state=state,
            var_file=_TRACE_PLACEHOLDER,
            targets=targets,
            replaces=replaces,
            extra=extra,
        ),
        "apply": lambda: commands.build_apply_argv(config, state=state, var_file=_TRACE_PLACEHOLDER, extra=extra),
        "refresh": lambda: commands.build_refresh_argv(
            config, state=state, var_file=_TRACE_PLACEHOLDER, targets=targets, extra=extra
        ),
        "destroy": lambda: commands.build_destroy_argv(
            config, state=state, var_file=_TRACE_PLACEHOLDER, targets=targets, extra=extra
        ),
        "output": lambda: commands.build_output_argv(config, state=state, extra=extra),
        "import": lambda: commands.build_import_argv(
            config,
            state=state,
            var_file=_TRACE_PLACEHOLDER,
            address=import_address,
            resource_id=import_id,
            extra=extra,
        ),
    }
    if subcommand not in builders:
        raise SelfCommandError(f"Unknown subcommand {subcommand!r}. Choose from: {', '.join(sorted(builders))}.")
    return builders[subcommand]()


# ---- `tfp last` (last terraform invocation) -----------------------------------


def last_invocation_path(config: Config) -> pathlib.Path:
    return config.tmp_dir / "last.json"


def do_last_invocation(config: Config) -> dict[str, Any]:
    path = last_invocation_path(config)
    if not path.exists():
        raise SelfCommandError(f"No prior invocation recorded at {path}.")
    return json.loads(path.read_text())


# ---- `tfp self snapshot` ------------------------------------------------------


def do_self_snapshot(
    config: Config,
    *,
    dest: pathlib.Path | None = None,
) -> pathlib.Path:
    """Pull the current remote tfstate to a local file via `terraform state pull`."""
    import datetime

    from tf_project import terraform as terraform_mod

    state = _require_state_or_error(config)
    if dest is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = config.tmp_dir / f"snapshot-{ts}.tfstate"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(  # noqa: S603 — explicit invocation
            [
                config.terraform_binary,
                f"-chdir={state.source_root}",
                "state",
                "pull",
            ],
            capture_output=True,
            check=True,
            env=terraform_mod.merged_env(state.environ),
        )
    except FileNotFoundError as exc:
        raise SelfCommandError(f"{config.terraform_binary}: command not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr.decode("utf-8", "replace") or "").strip() or f"exit {exc.returncode}"
        raise SelfCommandError(f"terraform state pull failed: {detail}") from exc
    dest.write_bytes(result.stdout)
    return dest


# ---- Remote-state lock helpers ------------------------------------------------


class LockStatus(NamedTuple):
    """Backend-agnostic lock report surfaced to the CLI."""

    backend: str  # "azurerm" | "s3"
    locked: bool
    detail: str  # backend-specific one-line summary
    lock_id: str | None = None
    lock_who: str | None = None
    lock_operation: str | None = None
    lock_created: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class BreakResult:
    method: str  # "force-unlock" | "lease-break"
    backend: str
    lock_id: str | None = None


def _require_state_or_error(config: Config) -> MyState:
    state = MyState.load(config)
    if state is None:
        raise SelfCommandError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return state


def _select_provider(state: MyState) -> lock.LockProvider:
    try:
        return lock.select_provider(state)
    except lock.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc


def do_self_lock_status(config: Config) -> LockStatus:
    """Backend-agnostic lock status: dispatches to the right provider."""
    state = _require_state_or_error(config)
    provider = _select_provider(state)
    try:
        report = provider.status()
    except lock.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc
    info = report.info
    return LockStatus(
        backend=report.backend,
        locked=report.locked,
        detail=report.detail,
        lock_id=info.id,
        lock_who=info.who,
        lock_operation=info.operation,
        lock_created=info.created,
    )


def do_self_lock_break(config: Config, *, blunt: bool = False) -> BreakResult:
    """Release the remote-state lock.

    Default (polite) path: discover the lock ID and run `terraform force-unlock
    <ID>` so terraform itself releases the lease and clears its metadata.

    Fallback (blunt) path: break the lease at the backend level (azurerm)
    or delete the DynamoDB lock item (s3). Triggered when the polite path
    can't run (no ID recoverable, or terraform itself fails), or when
    `blunt=True` is passed explicitly.
    """
    from tf_project import terraform as terraform_mod

    state = _require_state_or_error(config)
    provider = _select_provider(state)

    if not blunt:
        try:
            report = provider.status()
            if report.info.id:
                try:
                    terraform_mod.run(
                        [
                            config.terraform_binary,
                            f"-chdir={state.source_root}",
                            "force-unlock",
                            "-force",
                            report.info.id,
                        ],
                        env=terraform_mod.merged_env(state.environ),
                    )
                except terraform_mod.TerraformExit:
                    pass  # fall through to blunt break
                else:
                    return BreakResult(
                        method="force-unlock",
                        backend=provider.backend,
                        lock_id=report.info.id,
                    )
        except lock.LockProviderError:
            pass  # fall through to blunt break

    try:
        provider.break_lease()
    except lock.LockProviderError as exc:
        raise SelfCommandError(str(exc)) from exc
    return BreakResult(method="lease-break", backend=provider.backend)


__all__ = [
    "BreakResult",
    "DoctorCheck",
    "LockStatus",
    "SelfCommandError",
    "do_last_invocation",
    "do_self_banner_check",
    "do_self_config_path",
    "do_self_config_print",
    "do_self_doctor",
    "do_self_init",
    "do_self_lock_break",
    "do_self_lock_status",
    "do_self_snapshot",
    "do_self_state_clear",
    "do_self_state_show",
    "do_self_trace",
    "last_invocation_path",
]
