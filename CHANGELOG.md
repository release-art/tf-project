# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `tfp import <ADDRESS> <ID>` — promoted from raw passthrough to a wrapped
  subcommand. Forwards the decrypted tfvars (`-var-file`) and the saved
  env so provider blocks that reference variables can resolve. Extra
  terraform flags after the positional args still passthrough.
- `tfp self lock` now supports the **AWS S3 + DynamoDB** backend in addition
  to azurerm. Backend is auto-detected from the saved init state's
  `backend_config` (azurerm = `storage_account_name`+`container_name`,
  s3 = `bucket`+`dynamodb_table`).
- `tfp self lock break` is now **polite-by-default**: it discovers the lock
  ID and runs `terraform force-unlock <ID>` first, falling back to a
  backend-level lease break only if the polite path fails or the lock ID
  isn't recoverable. Pass `--blunt` to skip terraform entirely.
- `tfp self snapshot` — pulls the remote tfstate to a local file via
  `terraform state pull`. Defaults to `<tmp_dir>/snapshot-<UTC-timestamp>.tfstate`.
- `tfp self trace <subcommand>` — prints the exact terraform argv `tfp <subcommand>`
  would build, without invoking anything. Useful when copy-pasting a
  command to debug outside the wrapper.
- `tfp last` — prints the most recent terraform invocation recorded by
  tfp (argv + exit code + timestamp). Persisted at `<tmp_dir>/last.json`.
- Tab-completion for the `tfvars` argument of `tfp init` and
  `tfp self banner check`. Suggestions are annotated with the project name
  from each tfvars banner.
- Apply concurrency lock: `tfp apply`, `tfp refresh`, `tfp destroy` now take
  a non-blocking POSIX flock on `<tmp_dir>/apply.lock` and refuse to run if
  another invocation is already in progress.
- Stale-tfplan guard now also covers the **terraform source tree** under
  `source_root`, not just the tfvars file. Editing a `.tf` between plan
  and apply invalidates the saved plan.
- `tfp init` now prompts before overwriting the saved state with a
  different tfvars path. Pass `--force` to skip the prompt.
- `tfp self lock status` and `tfp self lock break` — inspect or release the
  remote-state lock. `status` also decodes the `terraformlockid`
  blob metadata so the lock ID is visible without having to provoke a
  failed terraform run; pair it with `tfp force-unlock <ID>` for the
  polite release path.
- CI: `twine check --strict` on the built sdist + wheel during lint, so
  PyPI metadata + README rendering regressions fail loudly before publish.
- Release: a `gh-release` job extracts the `[Unreleased]` section of
  CHANGELOG.md and creates a GitHub Release with those notes.

### Changed
- `LockStatus` is now backend-agnostic: `backend` + `locked` + `detail`
  string + `lock_id` / `lock_who` / `lock_operation` / `lock_created`.
  The Azure-specific `lease_state` / `lease_duration` fields are folded
  into `detail`.
- Tfplan meta sidecar field renamed from `tfvars_sha256` to `inputs_sha256`
  (the legacy field name is still accepted on read for forward-compat).
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
