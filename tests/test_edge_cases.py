"""Edge-case / error-branch coverage across multiple modules.

These tests fill in the narrow `if/raise/except` paths that the main test
files focus too coarsely to hit. Kept as one file (not per-module) because
each test is small and the focus is "hit branch X" rather than testing a
cohesive area.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

from tf_project.config import Config

# ---- state.py ---------------------------------------------------------------


def test_try_exclusive_lock_raises_when_busy(tmp_path: pathlib.Path) -> None:
    """A second non-blocking acquire on the same path raises LockBusyError."""
    from tf_project.state import LockBusyError, try_exclusive_lock

    path = tmp_path / "thing.lock"
    with try_exclusive_lock(path):
        with pytest.raises(LockBusyError):
            with try_exclusive_lock(path):
                pass


def test_write_active_cleans_up_tmp_on_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """If `Path.replace` fails, the tmp sibling is unlinked."""
    from tf_project.state import write_active

    orig_replace = pathlib.Path.replace
    tmp_seen: list[pathlib.Path] = []

    def boom_replace(self: pathlib.Path, target: pathlib.Path) -> pathlib.Path:
        tmp_seen.append(self)
        raise OSError("simulated")

    monkeypatch.setattr(pathlib.Path, "replace", boom_replace)
    with pytest.raises(OSError, match="simulated"):
        write_active(config, "dev")
    monkeypatch.setattr(pathlib.Path, "replace", orig_replace)
    assert tmp_seen and not tmp_seen[0].exists()


def test_clear_active(config: Config) -> None:
    from tf_project.state import clear_active, read_active, write_active

    write_active(config, "dev")
    assert read_active(config) == "dev"
    clear_active(config)
    assert read_active(config) is None
    # Idempotent.
    clear_active(config)


# ---- secrets.py -------------------------------------------------------------


def test_command_provider_rejects_empty_command() -> None:
    from tf_project.secrets import CommandProvider

    with pytest.raises(ValueError, match="non-empty"):
        CommandProvider(())


def test_command_provider_missing_src(tmp_path: pathlib.Path) -> None:
    from tf_project.secrets import CommandProvider

    provider = CommandProvider(("echo", "{in}", "{out}"))
    with pytest.raises(FileNotFoundError):
        with provider.materialize(tmp_path / "does-not-exist.tfvars"):
            pass


def test_provider_from_config_returns_command_when_set() -> None:
    from tf_project.config import SecretsConfig
    from tf_project.secrets import CommandProvider, provider_from_config

    cfg = SecretsConfig(command=("op", "inject"))
    assert isinstance(provider_from_config(cfg), CommandProvider)


def test_shred_on_nonexistent_path_is_silent(tmp_path: pathlib.Path) -> None:
    """`_shred` swallows OSError and the missing-file unlink."""
    from tf_project.secrets import _shred

    _shred(tmp_path / "not-there")  # no exception


# ---- models.py --------------------------------------------------------------


def test_versioned_model_top_level_not_object(tmp_path: pathlib.Path) -> None:
    """`load_file` rejects non-object JSON at the top level."""
    from tf_project.models import MyState, SchemaError

    p = tmp_path / "x.json"
    p.write_text('"a string"')
    with pytest.raises(SchemaError):
        MyState.load_file(p)


def test_versioned_model_migrate_called_for_older(tmp_path: pathlib.Path) -> None:
    """When `from_version < SUPPORTED_VERSION`, `migrate` is invoked."""
    from typing import Any, ClassVar

    from tf_project.models import VersionedModel

    class Toy(VersionedModel):
        SUPPORTED_VERSION: ClassVar[int] = 2
        schema_version: int = 2
        field: str

        @classmethod
        def migrate(cls, raw: dict[str, Any], *, from_version: int) -> dict[str, Any]:
            raw["field"] = "migrated"
            raw["schema_version"] = 2
            return raw

    p = tmp_path / "toy.json"
    p.write_text(json.dumps({"schema_version": 1, "field": "old"}))
    loaded = Toy.load_file(p)
    assert loaded.field == "migrated"


def test_dump_file_cleans_up_tmp_on_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-write crash unlinks the tmp file."""
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tmp_path / "x.tfvars",
        source_root=tmp_path / "src",
        tfplan_location=tmp_path / "plan",
    )
    target = tmp_path / "state.json"
    seen: list[pathlib.Path] = []

    def boom(self: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
        seen.append(self)
        raise OSError("simulated")

    monkeypatch.setattr(pathlib.Path, "replace", boom)
    with pytest.raises(OSError):
        state.dump_file(target)
    assert seen and not seen[0].exists()


# ---- lock.py ----------------------------------------------------------------


def test_azure_lock_missing_backend_fields_raises(
    config: Config, tfvars: pathlib.Path
) -> None:
    """Azure provider raises if storage_account_name etc. aren't in backend_config."""
    from tf_project.lock import AzureLockProvider, LockProviderError
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={"key": "k"},  # missing account/container
    )
    provider = AzureLockProvider(state)
    with pytest.raises(LockProviderError, match="storage_account_name"):
        provider.status()


def test_s3_lock_missing_backend_fields_raises(
    config: Config, tfvars: pathlib.Path
) -> None:
    """S3 provider raises if dynamodb_table is missing."""
    from tf_project.lock import LockProviderError, S3LockProvider
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={"key": "k", "bucket": "b"},  # no dynamodb_table
    )
    provider = S3LockProvider(state)
    with pytest.raises(LockProviderError, match="dynamodb_table"):
        provider.status()


def test_s3_decode_lock_info_invalid_json() -> None:
    """`_decode_s3_lock_info` returns empty LockInfo on unparseable input."""
    from tf_project.lock import _decode_s3_lock_info

    info = _decode_s3_lock_info("not json at all")
    assert info.id is None


def test_lock_info_empty_property() -> None:
    """LockInfo.empty is True iff id is None."""
    from tf_project.lock import LockInfo

    assert LockInfo().empty
    assert not LockInfo(id="x").empty


def test_select_provider_rejects_s3_without_dynamodb(
    config: Config, tfvars: pathlib.Path
) -> None:
    """S3 backend without DynamoDB → LockProviderError (terraform doesn't lock those)."""
    from tf_project.lock import LockProviderError, select_provider
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={"bucket": "b"},  # bucket present, dynamodb_table missing
    )
    with pytest.raises(LockProviderError, match="dynamodb_table"):
        select_provider(state)


# ---- config.py --------------------------------------------------------------


def test_config_missing_table_in_tf_project_toml(tmp_path: pathlib.Path) -> None:
    """`tf_project.toml` present but lacks the `[tf_project]` table → ConfigError."""
    from tf_project.config import ConfigError

    (tmp_path / "tf_project.toml").write_text("[wrong_table]\n")
    with pytest.raises(ConfigError, match="Missing"):
        Config.discover(tmp_path)


def test_config_terraform_binary_empty_string_rejected(tmp_path: pathlib.Path) -> None:
    from tf_project.config import ConfigError

    (tmp_path / "tf_project.toml").write_text(
        textwrap.dedent(
            """
            [tf_project]
            terraform_dir = "infra"
            terraform_binary = ""
            """
        )
    )
    with pytest.raises(ConfigError, match="non-empty"):
        Config.discover(tmp_path)


def test_config_terraform_binary_wrong_type_rejected(tmp_path: pathlib.Path) -> None:
    from tf_project.config import ConfigError

    (tmp_path / "tf_project.toml").write_text(
        textwrap.dedent(
            """
            [tf_project]
            terraform_dir = "infra"
            terraform_binary = 42
            """
        )
    )
    with pytest.raises(ConfigError):
        Config.discover(tmp_path)


def test_config_terraform_binary_absolute_path_kept(tmp_path: pathlib.Path) -> None:
    """An absolute terraform_binary path is used as-is."""
    abs_path = tmp_path / "tofu"
    abs_path.write_text("#!/bin/sh\n")
    (tmp_path / "tf_project.toml").write_text(
        textwrap.dedent(
            f"""
            [tf_project]
            terraform_dir = "infra"
            terraform_binary = "{abs_path}"
            """
        )
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_binary == str(abs_path)


# ---- migrate.py -------------------------------------------------------------


def test_migrate_skips_when_legacy_top_level_not_object(config: Config) -> None:
    """Legacy file containing a JSON list (not dict) is aborted cleanly."""
    from tf_project import migrate

    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text("[]")
    result = migrate.run_if_needed(config)
    assert result.migrated is False


def test_migrate_aborts_when_legacy_banner_unreadable(
    config: Config, tfvars: pathlib.Path
) -> None:
    """If the referenced tfvars has no parseable banner, migration aborts."""
    from tf_project import migrate

    # tfvars exists but has no banner.
    tfvars.write_text('foo = "bar"\n')
    legacy = {
        "tfvars": str(tfvars),
        "source_root": str(config.terraform_dir / "demo"),
        "tfplan_location": str(config.tmp_dir / "plan"),
        "environ": {},
        "backend_config": {},
    }
    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(json.dumps(legacy))
    result = migrate.run_if_needed(config)
    assert result.migrated is False
    assert result.note is not None and "banner" in result.note


def test_migrate_moves_legacy_tfplan(
    config: Config, tfvars: pathlib.Path
) -> None:
    """A legacy `my.tfplan` (and stale `.meta.json`) get relocated under the slot."""
    from tf_project import migrate

    legacy_state = {
        "tfvars": str(tfvars),
        "source_root": str(config.terraform_dir / "demo"),
        "tfplan_location": str(config.tmp_dir / "my.tfplan"),
        "environ": {},
        "backend_config": {},
    }
    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(json.dumps(legacy_state))
    (config.tmp_dir / migrate.LEGACY_TFPLAN_FILENAME).write_bytes(b"oldplan")
    (config.tmp_dir / (migrate.LEGACY_TFPLAN_FILENAME + ".meta.json")).write_text("{}")
    result = migrate.run_if_needed(config)
    assert result.migrated is True
    assert config.tfplan_file_for("dev").exists()
    assert config.tfplan_file_for("dev").read_bytes() == b"oldplan"


# ---- terraform.py -----------------------------------------------------------


def test_record_invocation_swallows_oserror(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_record_invocation` is best-effort: an OSError during dump_file is silenced."""
    from tf_project import terraform

    last_path = tmp_path / "last.json"
    terraform.set_runtime_options(last_invocation_path=last_path)

    def boom(self, path: pathlib.Path) -> None:  # type: ignore[no-untyped-def]
        raise OSError("simulated")

    monkeypatch.setattr("tf_project.models.LastInvocation.dump_file", boom)
    # No exception should escape.
    terraform._record_invocation(["terraform", "version"], exit_code=0)
    terraform.set_runtime_options(last_invocation_path=None)


def test_record_invocation_skipped_when_no_path() -> None:
    """If runtime options carry no `last_invocation_path`, record is a no-op."""
    from tf_project import terraform

    terraform.set_runtime_options(last_invocation_path=None)
    terraform._record_invocation(["terraform", "version"], exit_code=0)


def test_run_records_127_on_file_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """When the binary is missing, `run` records exit_code=127 before re-raising."""
    from tf_project import terraform

    last_path = tmp_path / "last.json"
    terraform.set_runtime_options(last_invocation_path=last_path)

    def boom(*a: object, **kw: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(terraform.subprocess, "Popen", boom)
    with pytest.raises(terraform.TerraformExit) as excinfo:
        terraform.run(["nope"])
    assert excinfo.value.code == 127
    # The record was attempted (with exit_code=127).
    assert last_path.exists()
    terraform.set_runtime_options(last_invocation_path=None)


def test_exec_passthrough_dry_run_skips(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """In dry-run mode, exec_passthrough prints the argv and returns instead of execing."""
    from tf_project import terraform

    called: list[object] = []
    monkeypatch.setattr(terraform.os, "execvp", lambda *a, **kw: called.append(a))
    terraform.set_runtime_options(dry_run=True)
    terraform.exec_passthrough(["terraform", "version"])
    assert called == []
    assert "[dry-run]" in capsys.readouterr().err
    terraform.set_runtime_options(dry_run=False)


def test_merged_env_no_overrides() -> None:
    """`merged_env({})` returns a copy of os.environ with no extras."""
    from tf_project.terraform import merged_env

    out = merged_env({})
    assert "TF_DATA_DIR" not in out
    assert "TF_PLUGIN_CACHE_DIR" not in out


# ---- slots.py ---------------------------------------------------------------


def test_resolved_slot_computed_fields(config: Config, tfvars: pathlib.Path) -> None:
    """ResolvedSlot's `computed_field` properties derive paths from slot_dir."""
    from tf_project.slots import resolve_slot

    slot = resolve_slot("dev", config=config)
    assert slot.data_dir == slot.slot_dir / ".terraform"
    assert slot.state_file == slot.slot_dir / "state.json"
    assert slot.tfplan_file == slot.slot_dir / "tfplan"
    assert slot.tfplan_meta_file == slot.slot_dir / "tfplan.meta.json"
    assert slot.backend_hash_file == slot.slot_dir / "backend.hash"


def test_resolve_slot_by_relative_path(
    config: Config, tfvars: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative tfvars path is resolved against cwd."""
    from tf_project.slots import resolve_slot

    monkeypatch.chdir(tfvars.parent.parent)
    rel = tfvars.relative_to(pathlib.Path.cwd())
    slot = resolve_slot(str(rel), config=config)
    assert slot.name == "dev"


def test_resolve_slot_missing_file(config: Config) -> None:
    """Absolute path to a missing tfvars raises SlotError."""
    from tf_project.slots import SlotError, resolve_slot

    with pytest.raises(SlotError, match="not found"):
        resolve_slot("/nope/missing.tfvars", config=config)


def test_list_slots_returns_empty_when_dir_missing(tmp_path: pathlib.Path) -> None:
    """list_slots tolerates a missing tfvars_dir (returns [])."""
    from tf_project.config import SecretsConfig
    from tf_project.slots import list_slots

    cfg = Config(
        project_root=tmp_path,
        terraform_dir=tmp_path / "tf",
        tfvars_dir=tmp_path / "nope",  # doesn't exist
        tmp_dir=tmp_path / "tmp",
        secrets=SecretsConfig(command=()),
    )
    assert list_slots(cfg) == []


def test_list_orphans_skips_files_at_slot_root(config: Config) -> None:
    """`list_orphans` ignores plain files under `slots_dir`."""
    from tf_project.slots import list_orphans

    config.slots_dir.mkdir(parents=True, exist_ok=True)
    (config.slots_dir / "stray.txt").write_text("not a slot")
    assert list_orphans(config) == []


def test_list_orphans_empty_when_slots_dir_missing(tmp_path: pathlib.Path) -> None:
    """`list_orphans` returns [] when `slots_dir` doesn't exist yet."""
    from tf_project.config import SecretsConfig
    from tf_project.slots import list_orphans

    cfg = Config(
        project_root=tmp_path,
        terraform_dir=tmp_path / "tf",
        tfvars_dir=tmp_path / "tfvars",
        tmp_dir=tmp_path / "tmp_does_not_exist",
        secrets=SecretsConfig(command=()),
    )
    assert list_orphans(cfg) == []


# ---- self_commands.py — small edge cases ------------------------------------


def test_self_doctor_secrets_disabled(config: Config) -> None:
    """When secrets.command is empty, doctor reports it as disabled."""
    from tf_project import self_commands

    checks = self_commands.do_self_doctor(config)
    names = {c.name: c for c in checks}
    assert "secrets command" in names
    assert names["secrets command"].ok is True
    assert "disabled" in names["secrets command"].detail


def test_self_doctor_secrets_command_missing(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the secrets binary isn't on PATH, doctor flags it."""
    from tf_project import self_commands
    from tf_project.config import SecretsConfig

    cfg = config.model_copy(update={"secrets": SecretsConfig(command=("bogus-cli",))})
    monkeypatch.setattr("tf_project.self_commands.shutil.which", lambda name: None)
    checks = self_commands.do_self_doctor(cfg)
    secrets_check = next(c for c in checks if "secrets command" in c.name)
    assert secrets_check.ok is False


def test_self_doctor_reports_slot_conflict(
    config: Config, project_tree: pathlib.Path
) -> None:
    """Doctor surfaces banner-uniqueness violations as a failed check."""
    from tf_project import self_commands

    # Add a duplicate-named tfvars.
    (project_tree / "tfvars" / "dup.tfvars").write_text(
        '# {"header":"terraform","project":"demo","slot":"dev"}\nfoo="bar"\n'
    )
    checks = self_commands.do_self_doctor(config)
    uniq = next(c for c in checks if "uniqueness" in c.name)
    assert uniq.ok is False


def test_self_doctor_reports_orphans(config: Config) -> None:
    """Doctor surfaces orphan slot dirs."""
    from tf_project import self_commands

    (config.slot_dir_for("ghost")).mkdir(parents=True)
    checks = self_commands.do_self_doctor(config)
    orph = next(c for c in checks if "orphan" in c.name)
    assert orph.ok is False
    assert "ghost" in orph.detail


def test_self_config_print_with_plugin_cache_disabled(
    project_tree: pathlib.Path,
) -> None:
    """`plugin_cache_dir = ""` shows up as "(disabled)" in the printed config."""
    from tf_project import self_commands

    (project_tree / "tf_project.toml").write_text(
        textwrap.dedent(
            """
            [tf_project]
            terraform_dir = "terraform"
            tfvars_dir = "tfvars"
            tmp_dir = "tmp"
            plugin_cache_dir = ""

            [tf_project.secrets]
            command = []
            """
        )
    )
    cfg = Config.discover(project_tree)
    out = self_commands.do_self_config_print(cfg, as_json=False)
    assert "(disabled)" in out


def test_self_state_clear_when_active_slot_has_no_state(config: Config) -> None:
    """`do_self_state_clear` on an active slot with no on-disk state returns []."""
    from tf_project import self_commands
    from tf_project.state import write_active

    write_active(config, "dev")
    removed = self_commands.do_self_state_clear(config, slot=None, all_slots=False)
    assert removed == []


# ---- commands.py — narrow branches ------------------------------------------


def test_do_passthrough_no_active_slot_runs_bare(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tfp <unknown>` with no active slot runs terraform with neither -chdir nor env."""
    from tf_project import commands

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.exec_passthrough",
        lambda cmd, env=None: calls.append({"cmd": cmd, "env": env}),
    )
    commands.do_passthrough(config, ["version"])
    assert calls[0]["env"] is None
    assert "-chdir=" not in " ".join(calls[0]["cmd"])  # type: ignore[arg-type]


def test_decide_init_first_init(config: Config, tfvars: pathlib.Path) -> None:
    """`_decide_init` returns needs_init=True with 'first init' reason when data_dir is absent."""
    from tf_project import commands
    from tf_project.slots import resolve_slot

    resolved = resolve_slot("dev", config=config)
    decision = commands._decide_init(config, resolved, config.terraform_binary)
    assert decision.needs_init is True
    assert "first init" in decision.reason


def test_decide_init_missing_hash(config: Config, tfvars: pathlib.Path) -> None:
    """data_dir exists but backend.hash doesn't → 'missing backend.hash'."""
    from tf_project import commands
    from tf_project.slots import resolve_slot

    resolved = resolve_slot("dev", config=config)
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    decision = commands._decide_init(config, resolved, config.terraform_binary)
    assert decision.needs_init is True
    assert "missing backend.hash" in decision.reason


def test_decide_init_hash_mismatch(config: Config, tfvars: pathlib.Path) -> None:
    """data_dir + backend.hash exist but hash differs → 'backend_config changed'."""
    from tf_project import commands
    from tf_project.slots import resolve_slot

    resolved = resolve_slot("dev", config=config)
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    config.backend_hash_for("dev").write_text("stale-hash\n")
    decision = commands._decide_init(config, resolved, config.terraform_binary)
    assert decision.needs_init is True
    assert "backend_config changed" in decision.reason


def test_status_report_active_but_no_state(config: Config) -> None:
    """status: active pointer set but no state.json yet."""
    from tf_project import commands
    from tf_project.state import write_active

    write_active(config, "dev")
    report = commands.status_report(config)
    assert report.initialized is False
    assert report.slot == "dev"
    assert report.slot_source == "file"


def test_slot_listing_warm_marker(
    config: Config, tfvars: pathlib.Path, save_dev_state: object
) -> None:
    """slot_listing reports `warm=True` and an mtime once `.terraform/providers/` exists."""
    from tf_project import commands

    save_dev_state()  # type: ignore[operator]
    (config.data_dir_for("dev") / "providers").mkdir(parents=True)
    [s] = commands.slot_listing(config)
    assert s.warm is True
    assert s.warm_mtime is not None


def test_do_fmt_no_dirs_raises(tmp_path: pathlib.Path) -> None:
    """do_fmt raises FileNotFoundError when neither terraform_dir nor tfvars_dir exists."""
    from tf_project import commands
    from tf_project.config import SecretsConfig

    cfg = Config(
        project_root=tmp_path,
        terraform_dir=tmp_path / "missing-tf",
        tfvars_dir=tmp_path / "missing-vars",
        tmp_dir=tmp_path / "tmp",
        secrets=SecretsConfig(command=()),
    )
    with pytest.raises(FileNotFoundError, match="exists to format"):
        commands.do_fmt(cfg)


def test_require_slot_state_unknown_slot(config: Config) -> None:
    """Asking for a slot that no banner defines yields a SlotError via assert_slot_exists."""
    from tf_project import commands

    with pytest.raises(commands.StateNotInitializedError, match="ghost"):
        commands._require_slot_state(config, "ghost")


# ---- drift detection — each cause ------------------------------------------


def _seed_plan_with_meta(
    config: Config,
    save_dev_state: object,
    project: str = "demo",
    state_key: str = "k",
    backend_config: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Helper: write a tfplan + meta with the given drift fields."""
    from tf_project.models import TfplanMeta

    save_dev_state()  # type: ignore[operator]
    config.tfplan_file_for("dev").write_bytes(b"plan")
    # Compute current sha matching state's tfvars so the sha check passes.
    from tf_project.commands import _plan_inputs_sha256
    from tf_project.state import load_state

    state = load_state(config, "dev")
    assert state is not None
    sha = _plan_inputs_sha256(state, state.tfvars)
    meta = TfplanMeta(
        inputs_sha256=sha,
        project=project,
        state_key=state_key,
        backend_config=backend_config or {},
        env=env or {},
    )
    meta.dump_file(config.tfplan_meta_for("dev"))


def test_drift_project_changes(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """Apply refuses when meta.project differs from current resolved.project."""
    from tf_project import commands

    _seed_plan_with_meta(config, save_dev_state, project="other-project")
    with pytest.raises(commands.StaleTfplanError, match="project"):
        commands.do_apply(config)


def test_drift_state_key_changes(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """Apply refuses when state_key differs."""
    from tf_project import commands

    _seed_plan_with_meta(config, save_dev_state, state_key="different-key")
    with pytest.raises(commands.StaleTfplanError, match="state_key"):
        commands.do_apply(config)


def test_drift_backend_config_changes(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """Apply refuses when backend_config differs."""
    from tf_project import commands

    _seed_plan_with_meta(
        config,
        save_dev_state,
        state_key="terraform/azure/dev.tfstate",  # matches synthesised key
        backend_config={"resource_group_name": "rg"},
    )
    with pytest.raises(commands.StaleTfplanError, match="backend_config"):
        commands.do_apply(config)


def test_drift_env_changes(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """Apply refuses when banner env differs."""
    from tf_project import commands

    _seed_plan_with_meta(
        config,
        save_dev_state,
        state_key="terraform/azure/dev.tfstate",
        env={"NEW_VAR": "x"},
    )
    with pytest.raises(commands.StaleTfplanError, match="env"):
        commands.do_apply(config)


# ---- self_commands.py: last_invocation, do_self_config_path ----------------


def test_last_invocation_returns_payload(
    config: Config, tfvars: pathlib.Path, save_dev_state  # type: ignore[no-untyped-def]
) -> None:
    """`do_last_invocation` reads back the recorded payload."""
    from tf_project import self_commands
    from tf_project.models import LastInvocation

    save_dev_state()
    LastInvocation(
        timestamp="2026-05-22T00:00:00+00:00",
        argv=["terraform", "plan"],
        exit_code=0,
    ).dump_file(config.last_invocation_for("dev"))
    payload = self_commands.do_last_invocation(config, slot="dev")
    assert payload["argv"] == ["terraform", "plan"]
    assert payload["exit_code"] == 0


def test_last_invocation_no_record(config: Config) -> None:
    """`do_last_invocation` raises when nothing has been recorded for the slot."""
    from tf_project import self_commands

    with pytest.raises(self_commands.SelfCommandError, match="No prior invocation"):
        self_commands.do_last_invocation(config, slot="dev")


def test_do_self_config_path_no_config(tmp_path: pathlib.Path) -> None:
    """`do_self_config_path` raises ConfigNotFoundError when nothing is found."""
    from tf_project import self_commands
    from tf_project.config import ConfigNotFoundError

    # No tf_project.toml, no pyproject.toml with the table — should walk up to /
    # and raise. Confine search by chdir'ing into a deep tmp_path subtree.
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    with pytest.raises(ConfigNotFoundError):
        self_commands.do_self_config_path(deep)


def test_do_self_config_path_skips_pyproject_without_table(
    tmp_path: pathlib.Path,
) -> None:
    """A pyproject.toml without the tf_project table is skipped — search continues upward."""
    from tf_project import self_commands
    from tf_project.config import ConfigNotFoundError

    (tmp_path / "pyproject.toml").write_text("[project]\nname='other'\n")
    with pytest.raises(ConfigNotFoundError):
        self_commands.do_self_config_path(tmp_path)


# ---- migrate.py: more error branches ---------------------------------------


def test_migrate_aborts_when_legacy_state_key_not_string(config: Config) -> None:
    """Legacy state with non-string `tfvars` is aborted."""
    from tf_project import migrate

    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(json.dumps({"tfvars": 42}))
    result = migrate.run_if_needed(config)
    assert result.migrated is False
    assert result.note is not None and "tfvars" in result.note


# ---- models.py: default migrate -------------------------------------------


def test_versioned_model_default_migrate_returns_raw(tmp_path: pathlib.Path) -> None:
    """A subclass that doesn't override `migrate` falls through to the no-op default."""
    from typing import ClassVar

    from tf_project.models import VersionedModel

    class StaticToy(VersionedModel):
        SUPPORTED_VERSION: ClassVar[int] = 2
        schema_version: int = 2
        f: str

    p = tmp_path / "static.json"
    # A v1 payload that happens to already match the v2 shape — default
    # migrate just returns raw unchanged.
    p.write_text(json.dumps({"schema_version": 1, "f": "v"}))
    loaded = StaticToy.load_file(p)
    assert loaded.f == "v"
    assert loaded.schema_version == 1  # pydantic took the raw value as-is


# ---- banner.py: _format_validation_error fallback --------------------------


def test_banner_validation_error_no_loc(tmp_path: pathlib.Path) -> None:
    """A banner with only a `header` value gets a helpful multi-field error."""
    from tf_project import banner as banner_mod

    f = tmp_path / "x.tfvars"
    f.write_text('# {"header": "terraform"}\n')  # missing project
    with pytest.raises(banner_mod.BannerError) as exc:
        banner_mod.find_banner(f)
    # The formatted message includes the location (field name).
    assert "project" in str(exc.value)


# ---- cli.py: self_state_clear error surface --------------------------------


def test_cli_self_state_clear_surfaces_error(
    project_tree: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    """A SelfCommandError raised by do_self_state_clear → exit 1."""
    from typer.testing import CliRunner

    from tf_project import self_commands
    from tf_project.cli import app

    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    monkeypatch.setattr(
        "tf_project.cli.self_commands.do_self_state_clear",
        lambda *a, **kw: (_ for _ in ()).throw(self_commands.SelfCommandError("nope")),
    )
    runner = CliRunner()
    result = runner.invoke(app, ["self", "state", "clear"])
    assert result.exit_code == 1


# ---- __main__.py: package entry point --------------------------------------


def test_main_module_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the module under `runpy` exercises `__main__.py`."""
    import runpy
    import sys

    called: list[bool] = []
    monkeypatch.setattr("tf_project.cli.main", lambda: called.append(True))
    monkeypatch.setattr(sys, "argv", ["tfp"])
    runpy.run_module("tf_project", run_name="__main__")
    assert called == [True]


# ---- Remaining branches ----------------------------------------------------


def test_banner_skips_non_terraform_json_comment(tmp_path: pathlib.Path) -> None:
    """A `# {"foo": "bar"}` comment that's valid JSON but not a tf banner is skipped."""
    from tf_project import banner as banner_mod

    f = tmp_path / "x.tfvars"
    f.write_text(
        '# {"unrelated": "json"}\n'
        '# {"header": "terraform", "project": "demo"}\n'
        'foo = "bar"\n'
    )
    parsed = banner_mod.find_banner(f)
    assert parsed.project == "demo"


def test_complete_slot_filters_by_prefix(
    project_tree: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    """`_complete_slot` returns [] when no slot matches the prefix."""
    from tf_project import cli

    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    items = cli._complete_slot(None, None, "zzz-no-match-")  # type: ignore[arg-type]
    assert items == []


def test_do_refresh_runs(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """do_refresh dispatches to apply argv builder with `-var-file` and slot env."""
    from tf_project import commands

    save_dev_state()
    commands.do_refresh(config, targets=["x.y"])
    cmd = run_calls[0]["cmd"]  # type: ignore[index]
    assert "apply" in cmd
    assert "-target=x.y" in cmd


def test_do_output_runs(
    config: Config, tfvars: pathlib.Path, save_dev_state, run_calls: object  # type: ignore[no-untyped-def]
) -> None:
    """do_output spawns `terraform output -json`."""
    from tf_project import commands

    save_dev_state()
    commands.do_output(config)
    cmd = run_calls[0]["cmd"]  # type: ignore[index]
    assert "output" in cmd and "-json" in cmd


def test_self_state_show_no_state_for_named_slot(
    config: Config, tfvars: pathlib.Path
) -> None:
    """do_self_state_show with --slot pointing at a banner-defined slot that has no state."""
    from tf_project import self_commands

    with pytest.raises(self_commands.SelfCommandError, match="No state for slot"):
        self_commands.do_self_state_show(config, slot="dev", all_slots=False)


def test_resolve_slot_name_with_explicit_slot(config: Config) -> None:
    """`_resolve_slot_name` returns the flag value when one is passed."""
    from tf_project import self_commands

    assert self_commands._resolve_slot_name(config, slot="explicit") == "explicit"


def test_migrate_aborts_when_legacy_unreadable(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reading the legacy file raises OSError, migration aborts cleanly."""
    from tf_project import migrate

    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text("{}")  # placeholder

    def boom(self: pathlib.Path) -> str:
        raise OSError("simulated")

    monkeypatch.setattr(pathlib.Path, "read_text", boom)
    result = migrate.run_if_needed(config)
    assert result.migrated is False


def test_list_orphans_skips_banner_matching_dirs(
    config: Config, tfvars: pathlib.Path
) -> None:
    """A slot dir whose name matches a banner-defined slot is NOT an orphan."""
    from tf_project.slots import list_orphans

    config.slot_dir_for("dev").mkdir(parents=True)  # matches dev tfvars
    assert list_orphans(config) == []


def test_self_snapshot_with_explicit_dest(
    config: Config, tfvars: pathlib.Path, save_dev_state, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path  # type: ignore[no-untyped-def]
) -> None:
    """`do_self_snapshot(dest=...)` skips the timestamp-default branch."""
    from tf_project import self_commands

    save_dev_state()

    class R:
        stdout = b"x"
        stderr = b""
        returncode = 0

    monkeypatch.setattr("tf_project.self_commands.subprocess.run", lambda *a, **kw: R())
    explicit = tmp_path / "out" / "snap.tfstate"
    written = self_commands.do_self_snapshot(config, dest=explicit)
    assert written == explicit
    assert written.read_bytes() == b"x"


def test_do_use_with_plugin_cache_disabled(
    project_tree: pathlib.Path,
    write_tfp_toml,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cmd_use` skips plugin-cache mkdir when the cache is disabled."""
    from tf_project import commands

    write_tfp_toml(
        project_tree,
        body="""
        [tf_project]
        terraform_dir = "terraform"
        tfvars_dir = "tfvars"
        tmp_dir = "tmp"
        plugin_cache_dir = ""

        [tf_project.secrets]
        command = []
        """,
    )
    cfg = Config.discover(project_tree)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "tf_project.commands.terraform.run",
        lambda cmd, env=None: calls.append({"cmd": cmd, "env": env}),
    )
    commands.do_use(cfg, arg="dev")
    env = calls[0]["env"]
    assert isinstance(env, dict)
    assert "TF_PLUGIN_CACHE_DIR" not in env  # disabled → not set


def test_config_discover_skips_pyproject_without_table(tmp_path: pathlib.Path) -> None:
    """A pyproject.toml without the tf_project table is skipped; discover continues upward."""
    from tf_project.config import ConfigNotFoundError

    (tmp_path / "pyproject.toml").write_text("[project]\nname='other'\n")
    with pytest.raises(ConfigNotFoundError):
        Config.discover(tmp_path)


def test_config_absolute_paths_in_toml(tmp_path: pathlib.Path) -> None:
    """Paths declared with absolute values in TOML are kept as-is (not joined with project_root)."""
    abs_tf = tmp_path / "abs-terraform"
    (tmp_path / "tf_project.toml").write_text(
        textwrap.dedent(
            f"""
            [tf_project]
            terraform_dir = "{abs_tf}"
            tfvars_dir = "tfvars"
            tmp_dir = "tmp"
            """
        )
    )
    cfg = Config.discover(tmp_path)
    assert cfg.terraform_dir == abs_tf.resolve()


def test_complete_tfvars_skips_non_matching_prefix(
    project_tree: pathlib.Path, write_tfp_toml, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    """`_complete_tfvars` continues past paths whose display doesn't start with the prefix."""
    from tf_project import cli

    write_tfp_toml(project_tree)
    monkeypatch.chdir(project_tree)
    items = cli._complete_tfvars(None, None, "zzz-no-such-prefix-")  # type: ignore[arg-type]
    assert items == []


def test_migrate_aborts_when_environ_is_not_dict(config: Config, tfvars: pathlib.Path) -> None:
    """Legacy `environ` value of the wrong type is aborted cleanly."""
    from tf_project import migrate

    (config.tmp_dir / migrate.LEGACY_STATE_FILENAME).write_text(
        json.dumps({"tfvars": str(tfvars), "environ": "not-a-dict"})
    )
    result = migrate.run_if_needed(config)
    assert result.migrated is False
    assert result.note is not None and "environ" in result.note


def test_self_state_clear_all_skips_slots_without_state(
    config: Config, tfvars: pathlib.Path, project_tree: pathlib.Path
) -> None:
    """--all only removes state files that exist; slots with no state.json are skipped."""
    from tf_project import self_commands

    # Add a second tfvars (so list_slots sees 2 slots), but don't save state
    # for either. clear --all should return [] without exploding.
    (project_tree / "tfvars" / "prod.tfvars").write_text(
        '# {"header":"terraform","project":"demo","slot":"prod"}\nfoo="bar"\n'
    )
    removed = self_commands.do_self_state_clear(config, slot=None, all_slots=True)
    assert removed == []


def test_plan_inputs_sha256_skips_missing_source_root(
    config: Config, tfvars: pathlib.Path
) -> None:
    """The rolling hash works even if source_root doesn't exist on disk."""
    from tf_project.commands import _plan_inputs_sha256
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=pathlib.Path("/does/not/exist"),
        tfplan_location=pathlib.Path("/tmp/plan"),
    )
    # Doesn't raise; just skips the .tf walk.
    sha = _plan_inputs_sha256(state, tfvars)
    assert len(sha) == 64


def test_azure_blob_args_without_optional_fields(
    config: Config, tfvars: pathlib.Path
) -> None:
    """Azure provider builds blob_args without --resource-group / --subscription when absent."""
    from tf_project.lock import AzureLockProvider
    from tf_project.models import MyState

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},  # no ARM_SUBSCRIPTION_ID
        backend_config={
            "key": "k",
            "storage_account_name": "sa",
            "container_name": "c",
            # no resource_group_name
        },
    )
    provider = AzureLockProvider(state)
    args = provider._blob_args()
    assert "--resource-group" not in args
    assert "--subscription" not in args


def test_decode_azure_lock_metadata_non_dict_payload() -> None:
    """Base64-encoded JSON list (not dict) yields an empty LockInfo."""
    import base64

    from tf_project.lock import _decode_azure_lock_metadata

    encoded = base64.b64encode(b'["a", "b"]').decode("ascii")
    info = _decode_azure_lock_metadata(encoded)
    assert info.id is None


def test_decode_s3_lock_info_non_dict_payload() -> None:
    """A JSON list (not dict) yields an empty LockInfo."""
    from tf_project.lock import _decode_s3_lock_info

    info = _decode_s3_lock_info('["a", "b"]')
    assert info.id is None


def test_self_lock_break_status_raises_falls_through(
    config: Config, tfvars: pathlib.Path, save_dev_state, monkeypatch: pytest.MonkeyPatch  # type: ignore[no-untyped-def]
) -> None:
    """When `provider.status()` raises during the polite path, fall through to blunt."""
    from tf_project import (
        lock as lock_mod,
        self_commands,
    )

    # Save dev state with a lock-discoverable backend.
    from tf_project.models import MyState
    from tf_project.state import save_state, write_active

    state = MyState(
        slot="dev",
        project="demo",
        tfvars=tfvars,
        source_root=(config.terraform_dir / "demo").resolve(),
        tfplan_location=config.tfplan_file_for("dev"),
        environ={},
        backend_config={
            "key": "k",
            "storage_account_name": "sa",
            "container_name": "c",
        },
    )
    save_state(state, config)
    write_active(config, "dev")

    # status() raises; break_lease() succeeds.
    status_calls = 0

    def fake_status(self: object) -> object:
        nonlocal status_calls
        status_calls += 1
        raise lock_mod.LockProviderError("cli auth failed")

    monkeypatch.setattr(lock_mod.AzureLockProvider, "status", fake_status)
    monkeypatch.setattr(lock_mod.AzureLockProvider, "break_lease", lambda self: None)
    result = self_commands.do_self_lock_break(config)
    assert result.method == "lease-break"
    assert status_calls == 1
