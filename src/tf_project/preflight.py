"""Pre-execution checks that turn predictable disk-state damage into
actionable recovery messages.

Catches things like a deleted `.terraform/` data dir or a half-deleted
`tfplan`/`tfplan.meta.json` pair - situations where terraform would
otherwise exit with a cryptic error and the user wouldn't know which
`tfp` command repairs the state.

`do_use` is the recovery entry point: every check that flags a missing
on-disk artifact suggests rerunning `tfp use <slot>`, which is the
single command that re-runs `terraform init` and rebuilds the wrapper's
sidecar files.
"""

from __future__ import annotations

from tf_project.config import Config


class RecoveryError(RuntimeError):
    """A pre-flight check failed with a concrete recovery suggestion."""


def check_data_dir(config: Config, slot: str) -> None:
    """Refuse to run terraform under a slot whose `.terraform/` is gone.

    Mirrors the signal `_decide_init` uses: missing `.terraform/` means
    terraform considers itself uninitialized, regardless of whether the
    wrapper's `state.json` and `backend.hash` are still present.
    """
    data_dir = config.data_dir_for(slot)
    if not data_dir.is_dir():
        raise RecoveryError(
            f"slot {slot!r}: terraform data dir is missing at {data_dir}. "
            f"It was probably deleted. Run `tfp use {slot}` to reinitialize."
        )


def check_apply_plan(config: Config, slot: str) -> None:
    """Refuse `apply` if the saved plan or its safety sidecar is gone.

    Both files are written together by `tfp plan` and removed together
    by a successful `tfp apply`. A half-present pair means the user
    deleted one by hand; running `apply` would either fail cryptically
    (plan missing) or silently skip the apply-safety drift check (meta
    missing). Force them to re-plan instead.
    """
    plan = config.tfplan_file_for(slot)
    meta = config.tfplan_meta_for(slot)
    if not plan.exists() and not meta.exists():
        raise RecoveryError(f"slot {slot!r}: no saved tfplan at {plan}. Run `tfp plan` first.")
    if plan.exists() != meta.exists():
        present = plan if plan.exists() else meta
        missing = meta if plan.exists() else plan
        raise RecoveryError(
            f"slot {slot!r}: saved plan is half-deleted ({present.name} present, "
            f"{missing.name} missing). Re-run `tfp plan` to regenerate both."
        )


__all__ = [
    "RecoveryError",
    "check_apply_plan",
    "check_data_dir",
]
