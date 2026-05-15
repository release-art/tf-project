"""Remote-state lock providers (azurerm + s3+dynamodb).

Each provider takes a `MyState` and knows how to:
 - report lease/lock status (incl. the terraform lock ID, if recoverable)
 - break the lease at the backend level (blunt path, last resort)

The polite path — `terraform force-unlock <ID>` — lives in commands.py and
calls into a provider only to discover the lock ID.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import json
import subprocess
from typing import Any, Protocol

from tf_project.state import MyState


class LockProviderError(RuntimeError):
    """User-visible failure inside a lock provider."""


@dataclasses.dataclass(frozen=True, slots=True)
class LockInfo:
    """Decoded terraform lock metadata (subset of fields)."""

    id: str | None = None
    who: str | None = None
    operation: str | None = None
    created: str | None = None

    @property
    def empty(self) -> bool:
        return self.id is None


@dataclasses.dataclass(frozen=True, slots=True)
class LockStatus:
    backend: str  # "azurerm" | "s3"
    locked: bool
    detail: str  # backend-specific one-line summary (lease state, etc.)
    info: LockInfo


class LockProvider(Protocol):
    backend: str

    def status(self) -> LockStatus: ...
    def break_lease(self) -> None: ...


# ---- Azure (azurerm backend) --------------------------------------------------


class AzureLockProvider:
    backend = "azurerm"

    def __init__(self, state: MyState) -> None:
        self._state = state

    def _blob_args(self) -> list[str]:
        bc = self._state.backend_config
        account = bc.get("storage_account_name")
        container = bc.get("container_name")
        key = bc.get("key")
        if not (account and container and key):
            missing = [
                n
                for n, v in (
                    ("storage_account_name", account),
                    ("container_name", container),
                    ("key", key),
                )
                if not v
            ]
            raise LockProviderError(f"Azure backend metadata missing from saved state: {', '.join(missing)}.")
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
        if sub := self._state.environ.get("ARM_SUBSCRIPTION_ID"):
            args.extend(["--subscription", sub])
        return args

    def status(self) -> LockStatus:
        out = _run_cli(["az", "storage", "blob", "show", *self._blob_args(), "-o", "json"])
        data = json.loads(out or "{}") or {}
        lease = (data.get("properties") or {}).get("lease") or {}
        metadata = data.get("metadata") or {}
        info = _decode_azure_lock_metadata(metadata.get("terraformlockid") or metadata.get("Terraformlockid"))
        lease_state = lease.get("state") or "unknown"
        lease_duration = lease.get("duration")
        detail = f"lease_state={lease_state}"
        if lease_duration:
            detail += f" lease_duration={lease_duration}"
        return LockStatus(
            backend=self.backend,
            locked=(lease.get("status") == "locked"),
            detail=detail,
            info=info,
        )

    def break_lease(self) -> None:
        _run_cli(["az", "storage", "blob", "lease", "break", *self._blob_args()])


def _decode_azure_lock_metadata(value: str | None) -> LockInfo:
    if not value:
        return LockInfo()
    with contextlib.suppress(binascii.Error, ValueError, json.JSONDecodeError):
        decoded = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
        payload = json.loads(decoded)
        if isinstance(payload, dict):
            return LockInfo(
                id=payload.get("ID"),
                who=payload.get("Who"),
                operation=payload.get("Operation"),
                created=payload.get("Created"),
            )
    return LockInfo()


# ---- AWS S3 + DynamoDB --------------------------------------------------------


class S3LockProvider:
    backend = "s3"

    def __init__(self, state: MyState) -> None:
        self._state = state

    def _common(self) -> tuple[str, str, str, str | None]:
        bc = self._state.backend_config
        table = bc.get("dynamodb_table")
        bucket = bc.get("bucket")
        key = bc.get("key")
        if not (table and bucket and key):
            missing = [
                n
                for n, v in (
                    ("dynamodb_table", table),
                    ("bucket", bucket),
                    ("key", key),
                )
                if not v
            ]
            raise LockProviderError(
                f"S3 backend metadata missing from saved state: {', '.join(missing)}. "
                "Note: `tfp self lock` requires the DynamoDB-locking variant of the S3 backend."
            )
        region = (
            bc.get("region") or self._state.environ.get("AWS_REGION") or self._state.environ.get("AWS_DEFAULT_REGION")
        )
        return table, bucket, key, region

    def _item_key(self) -> str:
        _, bucket, key, _ = self._common()
        return f"{bucket}/{key}"

    def _region_args(self) -> list[str]:
        _, _, _, region = self._common()
        return ["--region", region] if region else []

    def status(self) -> LockStatus:
        table, _, _, _ = self._common()
        out = _run_cli(
            [
                "aws",
                "dynamodb",
                "get-item",
                "--table-name",
                table,
                "--key",
                json.dumps({"LockID": {"S": self._item_key()}}),
                "--output",
                "json",
                *self._region_args(),
            ]
        )
        data = json.loads(out or "{}") or {}
        item = data.get("Item") or {}
        info_attr = (item.get("Info") or {}).get("S")
        info = _decode_s3_lock_info(info_attr)
        locked = info.id is not None
        detail = "lock_present" if locked else "no_lock"
        return LockStatus(
            backend=self.backend,
            locked=locked,
            detail=detail,
            info=info,
        )

    def break_lease(self) -> None:
        table, _, _, _ = self._common()
        _run_cli(
            [
                "aws",
                "dynamodb",
                "delete-item",
                "--table-name",
                table,
                "--key",
                json.dumps({"LockID": {"S": self._item_key()}}),
                *self._region_args(),
            ]
        )


def _decode_s3_lock_info(raw: str | None) -> LockInfo:
    if not raw:
        return LockInfo()
    with contextlib.suppress(json.JSONDecodeError, TypeError):
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return LockInfo(
                id=payload.get("ID"),
                who=payload.get("Who"),
                operation=payload.get("Operation"),
                created=payload.get("Created"),
            )
    return LockInfo()


# ---- Provider selection -------------------------------------------------------


def select_provider(state: MyState) -> LockProvider:
    bc = state.backend_config
    if "storage_account_name" in bc and "container_name" in bc:
        return AzureLockProvider(state)
    if "dynamodb_table" in bc and "bucket" in bc:
        return S3LockProvider(state)
    if "bucket" in bc:
        raise LockProviderError(
            "S3 backend without `dynamodb_table` — terraform stores no lock for this backend; "
            "tfp self lock not applicable."
        )
    raise LockProviderError(
        "Unrecognised backend in saved state. `tfp self lock` supports azurerm and "
        "s3 (with `dynamodb_table`); detected backend_config keys: "
        f"{sorted(bc.keys())}."
    )


# ---- Shared CLI shell-out -----------------------------------------------------


def _run_cli(argv: list[str]) -> str:
    """Shell out to az / aws / etc., raising LockProviderError with a clean message."""
    try:
        result = subprocess.run(  # noqa: S603 — explicit invocation, argv validated by callers
            argv,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        cli_name = argv[0]
        hint = {
            "az": "https://learn.microsoft.com/cli/azure/install-azure-cli",
            "aws": "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
        }.get(cli_name, "")
        msg = f"`{cli_name}` not found on PATH."
        if hint:
            msg += f" Install: {hint}"
        raise LockProviderError(msg) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise LockProviderError(f"{argv[0]} failed: {detail}") from exc
    return result.stdout


def _summarise_info(info: LockInfo) -> dict[str, Any]:
    return {
        "id": info.id,
        "who": info.who,
        "operation": info.operation,
        "created": info.created,
    }
