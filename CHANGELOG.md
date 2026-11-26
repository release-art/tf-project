# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `tfp apply` now also refuses a plan when a shared module changed: the
  fingerprint covers every `.tf` and `.tf.json` under `terraform_dir`, not just
  the project's own directory. `.terraform/` directories are skipped.
- `tmp_dir` is made readable by its owner only (mode 0700 on macOS and Linux)
  on each `tfp use` and `tfp init`: saved plans and state snapshots hold
  secret values in plain text.

### Added
- `tfp self doctor` checks that git ignores `tmp_dir`.

## [0.3.3] - 2026-11-26

### Fixed
- **Windows:** every command that saves a slot's state failed with
  `state.json.lock: Access is denied. (os error 5)`. CI now runs `use`,
  `plan` and `apply` on a local-backend project on every platform.

## [0.3.2] - 2026-11-26

### Added
- **Scoop:** `scoop install release-art/tf-project` on Windows, from the
  `release-art/scoop-bucket` bucket, updated by each release like the
  Homebrew tap.

## [0.3.1] - 2026-11-26

### Added
- **Azure Key Vault.** `{{ az-kv://<vault>/<kind>/<name>[/<version>] }}`
  references in tfvars, following Key Vault's own URLs, read with Microsoft's
  Azure SDK for Rust: `secrets/` gives the value, `keys/` the public key as a
  JSON Web Key, and `certificates/` the public certificate as PEM. tfp signs
  in through the Azure CLI once per run and reads everything concurrently.
- **`secrets_config` in the banner**, keyed by scheme, configures each secret
  source. `az-kv` takes `tenant` and `subscription` (defaulting to the
  banner's `ARM_TENANT_ID` / `ARM_SUBSCRIPTION_ID`) and `vault_url` (for
  sovereign clouds). `op` takes none.
- `tfp self doctor` reports each secret scheme the tfvars use and whether its
  tool (`op`, `az`) is installed.
- **`[tf_project.programs]`** sets where `op`, `az` and `aws` are, for tools
  that aren't on `PATH`: a name looked up on `PATH`, or a path.
- **Intel Macs.** Releases now include a macOS x86_64 wheel and standalone
  archive, built on GitHub's Intel macOS runner.
- **Homebrew:** `brew install release-art/tap/tf-project` on macOS and Linux.
- **Documentation** in `docs/`: configuration, commands, secrets, safety and
  recovery, and development. The README is now a short introduction and tour.

### Changed
- **tfp resolves secret references itself.** It collects the
  `{{ scheme://… }}` references in a tfvars file, fetches each scheme's
  together, and writes the filled-in copy to a private directory that is
  shredded afterwards. `op://` references are read in a single `op inject`
  call, so values with newlines or quotes come back intact.
- References are found anywhere in a tfvars file, including inside
  multi-line strings and heredocs, with or without spaces inside the braces.
  Other `{{ … }}` (Go templates and the like) is left as written.
- A reference with an unknown scheme is an error naming the file and the
  scheme, instead of reaching Terraform unresolved.
- A tfvars file without references is used as it is: no copy, and no `op`
  call.
- `tfp self init` no longer writes a `[tf_project.secrets]` section.
- Release builds are stripped and link-time optimised, keeping the programs at
  about 5.5 MB with the Azure SDK included.

### Removed
- **`[tf_project.secrets] command`** is no longer used: tfp doesn't run
  custom whole-file commands any more. A config that still has it gets a
  warning that it can be deleted, and if the command's first word was a path
  to `op`, that path keeps being used until `programs.op` is set.

### Fixed
- Secret references whose names contain spaces
  (`{{ op://Vault/Fuji non-prod/Client Id }}`) were left unresolved in
  0.3.1rc1 and reached Terraform as literal text. They are resolved now.

## [0.3.0] - 2026-11-26

### Changed
- **Rewritten in Rust.** `tf-project` / `tfp` is now a single native program,
  still installed from PyPI (`pip install tf-project`). The wheels carry only
  the program (`py3-none-<platform>`), built for Linux x86_64 and arm64,
  macOS arm64 and Windows x86_64. Python is no longer needed at runtime and
  startup is instant.
- **No more implicit passthrough.** Unknown subcommands used to be forwarded
  to `terraform`; they are now an error. Use `tfp tf <args>` to run any
  Terraform command in the active slot (`tfp tf validate`,
  `tfp tf force-unlock <ID>`).
- Terraform flags may follow a wrapped subcommand in any order
  (`tfp plan -lock=false -t a`); `--` still works.
- `tfp tf` waits for Terraform instead of replacing itself with it, so
  `tfp last` records the real exit code for every invocation. Ctrl-C still
  reaches Terraform, and tfp exits with Terraform's code.
- The apply / state locks now work on Windows too.
- **Standalone downloads.** Each GitHub Release also carries the programs
  per platform (`tf-project-<version>-<target>.tar.gz`, `.zip` for Windows),
  Sigstore-signed, for installing without Python.
- Error messages are worded differently in places.
- `tfp self snapshot list` also shows each snapshot's time.

### Removed
- The Python API (`import tf_project`).
- The automatic migration of the pre-slot `tmp/my_terraform_state.json`
  layout. Upgrade through 0.2.x first if you still have one.
- Shell completion via typer's `--install-completion`.

## [0.2.3] - 2026-06-26

### Changed
- **Raised the minimum `typer` to `>=0.26.0`** (was `>=0.21.2`). That series
  vendors its own click fork (`typer._click`) and drops the standalone
  `click` dependency. Shell-completion callbacks are now wired through
  typer's native `autocompletion=` API returning `(value, help)` tuples, so
  `tfp` no longer imports the `click` package directly - matching the
  environment we lock and test against.

### Fixed
- **`tfp init` now re-binds the backend instead of reusing a stale one.**
  Previously `tfp init` fell through to a bare `terraform init`, which
  silently reused the backend already cached in `.terraform/` from the last
  `tfp use` - so editing a banner's `state_key` / `backend_config` and
  running `tfp init` left the slot pinned to the *old* remote state key
  with no warning (and `tfp status` kept reporting the stale key). `init`
  is now a wrapped command that re-resolves the live banner, runs
  `terraform init -reconfigure` with the freshly-resolved `-backend-config`,
  re-locks providers, and refreshes the saved slot state - the same backend
  binding `tfp use` performs. Unlike `use`, it operates on the active slot
  (honouring `-s` / `TFP_SLOT`) and does not move the active-slot pointer.
  `tfp self trace init` traces the resulting argv.
- **Banner `state_key` must be an absolute, normalised pure POSIX path.**
  tf-project is opinionated here: a `state_key` has to start with `/` and be
  already normalised. Relative keys, backslashes, `.`/`..` segments,
  redundant `//`, and trailing slashes are now rejected with a clear banner
  error rather than silently rewritten - so the remote-state blob a banner
  targets is unambiguous from the text alone. The one transformation applied
  to a valid key is stripping its single leading slash: the azurerm backend
  prepends the container name, so `/site-monitor.tfstate` previously produced
  a double-slashed blob name (`.../tfstates//site-monitor.tfstate`) and now
  resolves to `site-monitor.tfstate`.

## [0.2.2] - 2026-05-24

### Added
- **Auto-snapshot before state-mutating ops.** `tfp state rm`, `tfp state
  mv`, `tfp state replace-provider`, and `tfp import` now `terraform state
  pull` to `<tmp_dir>/snapshots/<slot>/<ts>-pre-<op>.tfstate` before
  running - a failed pull aborts the op rather than destroying state you
  can't roll back to. Recovery is `tfp self snapshot restore [<name>]`
  (`terraform state push -force`, confirmation prompt; `-y` to skip), and
  `tfp self snapshot list` enumerates per-slot snapshots newest first. New
  `snapshot_retention` config field bounds the per-slot count (default
  **200**, oldest-first pruning on every write; `0` disables auto-snapshot
  entirely). `apply` is deliberately excluded - its plan/refresh cycle
  already gives a recoverable diff and auto-snapshotting every apply would
  dominate iteration time.

### Changed
- **Cross-platform lockfile hashes are kept fresh automatically.** Every
  `tfp use` that runs `terraform init` now chases it with `terraform
  providers lock` for `linux_amd64` + `linux_arm64` (the common CI /
  runtime targets) plus the host's own platform - so the committed
  `.terraform.lock.hcl` is portable across the machines that actually
  touch it without the user remembering to re-lock. Multi-platform teams
  extend the set via the new `extra_lock_platforms` field in
  `tf_project.toml` (e.g. `["windows_amd64"]`); entries are validated at
  config-load time and deduped against the defaults. The previous
  "lockfile missing" warning is gone - no longer reachable. `tfp self
  providers lock [-p ...]` remains for ad-hoc re-locking.
- **`tfp self snapshot` is now a sub-app** (`create` / `list` / `restore`).
  The old `tfp self snapshot` leaf becomes `tfp self snapshot create`;
  snapshots live under `<tmp_dir>/snapshots/<slot>/` instead of
  `<tmp_dir>/snapshot-<ts>.tfstate` so retention can prune per-slot.

## [0.2.1] - 2026-05-24

### Added
- **Recovery preflight.** Wrapped commands now bail out with an actionable
  message when on-disk state is half-deleted, instead of forwarding a
  cryptic terraform error: a missing `.terraform/` data dir tells the user
  to `tfp use <slot>`, and a half-deleted `tfplan`/`tfplan.meta.json` pair
  tells the user to re-run `tfp plan`. `tfp use` additionally warns when
  `.terraform.lock.hcl` is missing while the plugin cache is on, pointing
  at `tfp self providers lock` to repopulate cross-platform hashes.

## [0.2.0] - 2026-05-22

### Release pipeline
- **Tag-driven releases.** The wheel's version is now derived from the git
  tag at build time via `pdm-backend`'s SCM source; `src/tf_project/__version__.py`
  reads the installed distribution's metadata at runtime. No more code-side
  version bump - push `vX.Y.Z` and the workflow takes over.
- **Sigstore-signed artifacts.** Every release attaches the `.whl`,
  `.tar.gz`, and matching `.sigstore` bundles to the GitHub Release.
- **GitHub Releases automated.** Changelog notes are extracted from
  `## [X.Y.Z]` sections by `mindsers/changelog-reader-action`; pre-release
  tags (`rcN`, `aN`, `bN`, `devN`) are auto-flagged.
- Removed the post-publish `pdm bump patch` step - the next release version
  is whatever you tag next, not whatever a bot guessed.

### Added - slot model (BREAKING)
- **Named slots replace the single-state model.** Each tfvars file is bound
  to a slot whose name defaults to the filename stem and can be overridden
  via a new banner field: `{"slot": "production"}`. State, tfplan, and
  `.terraform/` are stored per-slot under `tmp/slots/<slot>/`, so flipping
  between slots is now a pointer write - no `terraform init` when the slot
  is already warm.
- `tfp use <slot-or-path>` - sole verb for selecting a slot. Accepts a slot
  name or a path to a tfvars file (paths are convenient with shell
  completion). Runs `terraform init -reconfigure` only when the slot's
  `.terraform/` is missing or the backend signature has changed.
- `tfp ls` - list all slots derived from tfvars banners. Marks the active
  slot, shows project + backend key + warm-since mtime. `--json` for
  scripts. Orphans (slot dirs with no matching banner) are listed
  separately.
- `tfp rm <slot>` - delete a slot's on-disk directory. Refuses on the
  active slot unless `--force`. Never touches the tfvars file.
- Global `-s/--slot <name>` flag - operate against a specific slot for one
  invocation, without rewriting the saved active.
- `TFP_SLOT` env var - overrides the saved active for the current shell
  (useful for parallel slots across terminals or in CI).
- Banner `slot` field validated for uniqueness across `tfvars_dir/*.tfvars`
  on every `tfp use` / `tfp ls`. Conflicts name both files and whether each
  was explicit vs default.
- `TF_DATA_DIR` is set per slot, so two slots that share a project keep
  independent provider/module/backend bindings on disk.
- Plugin cache shared across all slots via `TF_PLUGIN_CACHE_DIR`
  (default `tmp/plugin-cache/`, configurable). Providers download once and
  are linked into each slot's `.terraform/providers/` - first init for a
  new slot is seconds, not minutes. `TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1`
  is set automatically; pair with `tfp self providers lock` to populate
  multi-platform hashes for CI.
- `tfp self providers lock [-p <platform>]` - wraps `terraform providers lock`
  across `linux/darwin × amd64/arm64` by default.
- `tfp self state show --all` / `tfp self state clear --all` - operate on
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

### Changed - slot model (BREAKING)
- `tfp init <tfvars>` is **removed**. Use `tfp use <slot-or-path>` instead.
- Banner `env` is no longer merged across `tfp use` invocations - the
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
- `tfp state` subcommand group - all eight terraform state subcommands
  (`list`, `show`, `mv`, `rm`, `pull`, `push`, `replace-provider`,
  `identities`) wrapped and discoverable in `tfp --help`. Each forwards
  `-chdir` + the saved env.
- `tfp import <ADDRESS> <ID>` - promoted from raw passthrough to a wrapped
  subcommand. Forwards the decrypted tfvars (`-var-file`) and the saved
  env so provider blocks that reference variables can resolve. Extra
  terraform flags after the positional args still passthrough.
- `tfp self lock` supports both the **azurerm** and **AWS S3 + DynamoDB**
  backends. Backend is auto-detected from the saved slot state's
  `backend_config` (azurerm = `storage_account_name`+`container_name`,
  s3 = `bucket`+`dynamodb_table`).
- `tfp self lock break` is **polite-by-default**: it discovers the lock
  ID and runs `terraform force-unlock <ID>` first, falling back to a
  backend-level lease break only if the polite path fails or the lock ID
  isn't recoverable. Pass `--blunt` to skip terraform entirely. Refuses
  to escalate to a backend-level break when no lock is present.
- `tfp self snapshot` - pulls the remote tfstate to a local file via
  `terraform state pull`. Defaults to `<tmp_dir>/snapshot-<UTC-timestamp>.tfstate`.
- `tfp self trace <subcommand>` - prints the exact terraform argv `tfp <subcommand>`
  would build, without invoking anything. Useful when copy-pasting a
  command to debug outside the wrapper. Tracing `use` works against a
  fresh slot - no saved state required.
- `tfp last` - prints the most recent terraform invocation recorded by
  tfp (argv + exit code + timestamp). Persisted per-slot at
  `tmp/slots/<slot>/last.json`.
- Tab-completion for `tfp use` (slot names + tfvars paths) and
  `tfp self banner check` (tfvars paths). Suggestions are annotated with
  the project name from each tfvars banner.
- Apply concurrency lock: `tfp apply`, `tfp refresh`, `tfp destroy` take
  a non-blocking POSIX flock per slot and refuse to run if another
  invocation is already in progress.
- Stale-tfplan guard covers the **terraform source tree** under
  `source_root`, not just the tfvars file. Editing a `.tf` between plan
  and apply invalidates the saved plan.
- `tfp self lock status` decodes the `terraformlockid` blob metadata so
  the lock ID is visible without having to provoke a failed terraform
  run; pair it with `tfp force-unlock <ID>` for the polite release path.
- CI: `twine check --strict` on the built sdist + wheel during lint, so
  PyPI metadata + README rendering regressions fail loudly before publish.

### Changed
- `LockStatus` is backend-agnostic: `backend` + `locked` + `detail` +
  `lock_id` / `lock_who` / `lock_operation` / `lock_created`. The
  Azure-specific `lease_state` / `lease_duration` fields are folded into
  `detail`.
- Tfplan meta sidecar field renamed from `tfvars_sha256` to
  `inputs_sha256`. The legacy field name is **not** accepted on read -
  saved plans from pre-rename versions must be re-created with
  `tfp plan`.
- `tfp status` - one-line summary of the active slot.
- `tfp self banner check <tfvars>` - validates a banner and prints the
  resolved `source_root` / `backend_config` / `env` / `slot` without
  running terraform.
- Global `--verbose` and `--dry-run` flags. `--verbose` logs the terraform
  argv before exec; `--dry-run` prints the argv and skips execution.
- `[tf_project.backend_config]` TOML table and matching `backend_config`
  banner field for arbitrary `-backend-config key=value` passthrough.
- Shell-completion entry point (Typer's built-in `--install-completion`).
- Real-terraform end-to-end integration test covering use→plan→apply→destroy
  against a `null_resource` fixture, plus a two-slot warm-switch test.
- `BannerError` for clean, file-pathed banner validation failures.
- `py.typed` marker - the package ships type information explicitly.
- Terraform exit codes propagate cleanly (no Python traceback when
  terraform fails). `terraform` not found exits 127.
- SIGINT during a running terraform command no longer kills Python first;
  terraform receives the signal via the shared process group and we wait
  for it to finish.
- Passthrough invocations (`tfp <unknown-subcommand>`) `os.execvpe` the
  terraform process directly - native signal handling and exit code.
  `tfp -s <slot> <unknown>` refuses if the slot has no saved state, rather
  than silently dropping `-chdir`.
- `state.save_state` takes an exclusive file lock so parallel `tfp use`
  runs can't clobber each other (POSIX only). State and active-pointer
  writes are atomic (write-to-tmp + `Path.replace`).
- Banner parsing lives in `tf_project.banner`; validation errors raise
  `BannerError` with the offending file path.

### Removed
- `tfp state-mv` top-level command. Use `tfp state mv` instead.
- `commands.do_state_mv` public helper. Use `commands.do_state(subcommand="mv", ...)`.

## [0.1.0] - 2026-05-14

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
