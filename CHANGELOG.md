# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `tfp self lock status` and `tfp self lock break` — inspect or break the
  Azure blob lease on the remote tfstate, for unsticking a state that was
  hard-killed mid-operation. `status` also decodes the `terraformlockid`
  blob metadata so the lock ID is visible without having to provoke a
  failed terraform run; pair it with `tfp force-unlock <ID>` for the
  polite release path. Shells out to `az storage blob`.
- `tfp status` — one-line summary of the current init state.
- `tfp self banner check <tfvars>` — validate a tfvars banner and print the
  resolved `source_root` / `backend_config` / `env` without running terraform.
- Global `--verbose` and `--dry-run` flags. `--verbose` logs the terraform
  argv before exec; `--dry-run` prints the argv and skips execution.
- `[tf_project.backend_config]` TOML table and matching `backend_config`
  banner field for arbitrary `-backend-config key=value` passthrough
  (resource_group_name, storage_account_name, etc.).
- Stale-tfplan guard: `tfp plan` writes a sidecar with the SHA-256 of the
  decrypted tfvars; `tfp apply` refuses to apply if the tfvars changed.
  Bypass with `tfp apply --force`.
- Shell-completion entry point (Typer's built-in `--install-completion`).
- Real-terraform end-to-end integration test covering init→plan→apply→destroy
  against a `null_resource` fixture.
- Subprocess smoke test exercising `python -m tf_project --version`.
- `BannerError` for clean, file-pathed banner validation failures.
- `py.typed` marker — the package now ships type information explicitly.

### Changed
- Terraform exit codes propagate cleanly (no Python traceback when
  terraform fails). `terraform` not found exits 127.
- SIGINT during a running terraform command no longer kills Python first;
  terraform receives the signal via the shared process group and we wait
  for it to finish.
- Passthrough invocations (`tfp <unknown-subcommand>`) now `os.execvpe` the
  terraform process directly — native signal handling and exit code.
- `MyState.save` takes an exclusive file lock so parallel `tfp init` runs
  can't clobber each other (POSIX only).
- Banner parsing moved into `tf_project.banner`; validation errors now
  raise `BannerError` with the offending file path.

## [0.1.0] — 2026-05-14

### Added
- Initial release. `tf-project` (aka `tfp`) CLI wrapping `terraform`
  init / plan / apply / refresh / destroy / fmt / output / state-mv with
  per-tfvars backend keys, pluggable secrets preprocessor, and a
  pyproject.toml-driven config.
- Unknown subcommands forwarded to `terraform` verbatim.
- `self` subcommand group: `init`, `config print|path`, `state show|clear`,
  `doctor`.
- Banner JSON support in tfvars: `project` (subdir), `state_key` (full
  remote-state key override), `env` (env vars merged into state).
- Configurable `terraform_binary` (resolved via `shutil.which` when unset).
