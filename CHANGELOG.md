# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Release pipeline
- **Tag-driven releases.** The wheel's version is now derived from the git
  tag at build time via `pdm-backend`'s SCM source; `src/tf_project/__version__.py`
  reads the installed distribution's metadata at runtime. No more code-side
  version bump — push `vX.Y.Z` and the workflow takes over.
- **Sigstore-signed artifacts.** Every release attaches the `.whl`,
  `.tar.gz`, and matching `.sigstore` bundles to the GitHub Release.
- **GitHub Releases automated.** Changelog notes are extracted from
  `## [X.Y.Z]` sections by `mindsers/changelog-reader-action`; pre-release
  tags (`rcN`, `aN`, `bN`, `devN`) are auto-flagged.
- Removed the post-publish `pdm bump patch` step — the next release version
  is whatever you tag next, not whatever a bot guessed.

### Added — slot model (BREAKING)
- **Named slots replace the single-state model.** Each tfvars file is bound
  to a slot whose name defaults to the filename stem and can be overridden
  via a new banner field: `{"slot": "production"}`. State, tfplan, and
  `.terraform/` are stored per-slot under `tmp/slots/<slot>/`, so flipping
  between slots is now a pointer write — no `terraform init` when the slot
  is already warm.
- `tfp use <slot-or-path>` — sole verb for selecting a slot. Accepts a slot
  name or a path to a tfvars file (paths are convenient with shell
  completion). Runs `terraform init -reconfigure` only when the slot's
  `.terraform/` is missing or the backend signature has changed.
- `tfp ls` — list all slots derived from tfvars banners. Marks the active
  slot, shows project + backend key + warm-since mtime. `--json` for
  scripts. Orphans (slot dirs with no matching banner) are listed
  separately.
- `tfp rm <slot>` — delete a slot's on-disk directory. Refuses on the
  active slot unless `--force`. Never touches the tfvars file.
- Global `-s/--slot <name>` flag — operate against a specific slot for one
  invocation, without rewriting the saved active.
- `TFP_SLOT` env var — overrides the saved active for the current shell
  (useful for parallel slots across terminals or in CI).
- Banner `slot` field validated for uniqueness across `tfvars_dir/*.tfvars`
  on every `tfp use` / `tfp ls`. Conflicts name both files and whether each
  was explicit vs default.
- `TF_DATA_DIR` is set per slot, so two slots that share a project keep
  independent provider/module/backend bindings on disk.
- Plugin cache shared across all slots via `TF_PLUGIN_CACHE_DIR`
  (default `tmp/plugin-cache/`, configurable). Providers download once and
  are linked into each slot's `.terraform/providers/` — first init for a
  new slot is seconds, not minutes. `TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1`
  is set automatically; pair with `tfp self providers lock` to populate
  multi-platform hashes for CI.
- `tfp self providers lock [-p <platform>]` — wraps `terraform providers lock`
  across `linux/darwin × amd64/arm64` by default.
- `tfp self state show --all` / `tfp self state clear --all` — operate on
  every slot at once.
- `tfp self doctor` adds slot-uniqueness and orphan checks.
- Apply-safety hashes now also pin `project`, `state_key`, full
  `backend_config`, and banner `env` into `tfplan.meta.json`. Banner edits
  that change the apply target invalidate the plan even when the tfvars
  body and `.tf` source are unchanged.
- Versioned state files: every persisted JSON carries `schema_version`
  (currently 1). `tfp` refuses to load files written by a newer version
  and walks older versions through a per-model migrator.
- One-shot legacy migration: `tmp/my_terraform_state.json` is promoted to
  `tmp/slots/<slot>/state.json` on first run, with a breadcrumb left at
  `tmp/MIGRATED-<UTC-timestamp>.txt`.

### Changed — slot model (BREAKING)
- `tfp init <tfvars>` is **removed**. Use `tfp use <slot-or-path>` instead.
- Banner `env` is no longer merged across `tfp use` invocations — the
  banner is the single source of truth at the moment of switching.
- `tfp last` / `tfp status` / `tfp self state show` / `tfp self trace` /
  `tfp self snapshot` / `tfp self lock` are all slot-scoped. They consult
  the active slot (or `-s/TFP_SLOT`) and operate within `slots/<slot>/`.
- All file I/O goes through pydantic models. Persisted models use
  `extra="forbid"`; the banner uses `extra="allow"` and logs unknown fields
  to stderr (forward-compatible but visible). Unknown fields in
  `tf_project.toml` now fail loudly.
- All runtime `pathlib.Path` values are absolute. Paths in TOML are
  resolved against `project_root` at load time; business logic does not
  re-resolve.

### Added
- `tfp state` subcommand group — all eight terraform state subcommands
  (`list`, `show`, `mv`, `rm`, `pull`, `push`, `replace-provider`,
  `identities`) wrapped and discoverable in `tfp --help`. Each forwards
  `-chdir` + the saved env.

### Removed
- `tfp state-mv` top-level command. Use `tfp state mv` instead.
- `commands.do_state_mv` public helper. Use `commands.do_state(subcommand="mv", ...)`.
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
