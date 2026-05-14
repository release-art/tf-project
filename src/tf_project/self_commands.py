"""Implementations behind `tfp self ...` subcommands."""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import json
import pathlib
import shutil
import subprocess
import tomllib
from typing import Any, NamedTuple

from tf_project import banner
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


# ---- Azure remote-state lock helpers -------------------------------------------


class LockStatus(NamedTuple):
    locked: bool
    lease_state: str  # available | leased | expired | breaking | broken | unknown
    lease_duration: str | None  # "infinite" | "fixed" | None
    # The following are populated from the blob's `terraformlockid` metadata
    # (base64-encoded JSON written by the azurerm backend). All `None` if the
    # metadata is missing or unparseable.
    lock_id: str | None = None
    lock_who: str | None = None
    lock_operation: str | None = None
    lock_created: str | None = None


def _decode_lock_info(value: str | None) -> dict[str, Any] | None:
    """Decode the terraformlockid blob-metadata value (base64-encoded JSON)."""
    if not value:
        return None
    with contextlib.suppress(binascii.Error, ValueError, json.JSONDecodeError):
        decoded = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
        payload = json.loads(decoded)
        if isinstance(payload, dict):
            return payload
    return None


def _require_state_or_error(config: Config) -> MyState:
    state = MyState.load(config)
    if state is None:
        raise SelfCommandError(f"No state at {config.state_file}. Run `tfp init <tfvars>` first.")
    return state


def _azure_blob_args(state: MyState) -> list[str]:
    """Build the common `az storage blob` argv tail from the saved init state."""
    bc = state.backend_config
    account = bc.get("storage_account_name")
    container = bc.get("container_name")
    key = bc.get("key")
    if not (account and container and key):
        missing = [
            name
            for name, value in (
                ("storage_account_name", account),
                ("container_name", container),
                ("key", key),
            )
            if not value
        ]
        raise SelfCommandError(
            f"Azure backend metadata missing from saved state: {', '.join(missing)}. "
            "Set them under [tf_project.backend_config] (or via banner.backend_config) and re-run `tfp init`."
        )
    args = [
        "--account-name",
        account,
        "--container-name",
        container,
        "--blob-name",
        key,
    ]
    if rg := bc.get("resource_group_name"):
        args.extend(["--resource-group", rg])
    if subscription := state.environ.get("ARM_SUBSCRIPTION_ID"):
        args.extend(["--subscription", subscription])
    return args


def _run_az(args: list[str], *, capture: bool = True) -> str:
    """Shell out to `az`, raising SelfCommandError with a clean message on failure."""
    try:
        result = subprocess.run(  # noqa: S603,S607 — explicit invocation, args validated by callers
            ["az", *args],
            capture_output=capture,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SelfCommandError(
            "`az` (Azure CLI) not found on PATH. "
            "Install it from https://learn.microsoft.com/cli/azure/install-azure-cli "
            "or break the lease manually in the Azure portal."
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise SelfCommandError(f"az failed: {detail}") from exc
    return result.stdout if capture else ""


def do_self_lock_status(config: Config) -> LockStatus:
    """Query the Azure blob lease + lock metadata for the current tfstate.

    Returns lease info (status / state / duration) plus the terraform lock
    info (ID / who / operation / created) which the azurerm backend writes
    to the blob's `terraformlockid` metadata as base64-encoded JSON.
    """
    state = _require_state_or_error(config)
    out = _run_az(
        [
            "storage",
            "blob",
            "show",
            *_azure_blob_args(state),
            "-o",
            "json",
        ]
    )
    data = json.loads(out or "{}") or {}
    lease = (data.get("properties") or {}).get("lease") or {}
    metadata = data.get("metadata") or {}
    # Metadata keys are case-insensitive in Azure but az preserves whatever
    # case terraform wrote. Look for the canonical key and a lower-cased one.
    lock_b64 = metadata.get("terraformlockid") or metadata.get("Terraformlockid")
    info = _decode_lock_info(lock_b64) or {}
    return LockStatus(
        locked=(lease.get("status") == "locked"),
        lease_state=lease.get("state") or "unknown",
        lease_duration=lease.get("duration"),
        lock_id=info.get("ID"),
        lock_who=info.get("Who"),
        lock_operation=info.get("Operation"),
        lock_created=info.get("Created"),
    )


def do_self_lock_break(config: Config) -> None:
    """Break the Azure blob lease on the current tfstate.

    Does not require the terraform lock ID — operates at the blob-lease level
    so it succeeds even after a hard kill while terraform was holding the
    lease.
    """
    state = _require_state_or_error(config)
    _run_az(
        ["storage", "blob", "lease", "break", *_azure_blob_args(state)],
        capture=True,
    )


__all__ = [
    "DoctorCheck",
    "LockStatus",
    "SelfCommandError",
    "do_self_banner_check",
    "do_self_config_path",
    "do_self_config_print",
    "do_self_doctor",
    "do_self_init",
    "do_self_lock_break",
    "do_self_lock_status",
    "do_self_state_clear",
    "do_self_state_show",
]
