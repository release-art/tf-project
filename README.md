# tf-project

A thin, opinionated Terraform-project wrapper. Provides a single `tf-project`
(aka `tfp`) CLI built around **named slots**: each tfvars file binds to a
slot, and switching between slots is a pointer write — not a re-`terraform init`.

- **Slots, not paths.** Banner-declared (or filename-derived) names refer to
  tfvars files. `tfp use dev` flips the active slot; `tfp plan` / `tfp apply`
  operate against it.
- **One `.terraform/` per slot** via `TF_DATA_DIR`. Two slots pointing at the
  same project keep independent provider/module/backend bindings on disk —
  swapping between them is instant when both are warm.
- **Shared provider cache** via `TF_PLUGIN_CACHE_DIR`. Providers download
  once and link into every slot's `.terraform/providers/`.
- **Versioned, pydantic-validated state files.** Every persisted JSON carries
  `schema_version`; unknown fields fail loudly (except in user-authored
  banners, where they are tolerated and logged).
- Pluggable tfvars preprocessing — defaults to [1Password's `op inject`][op]
  so you can keep `op://...` references in tfvars committed to git.

[op]: https://developer.1password.com/docs/cli/secrets-template-syntax/

## Install

```sh
pip install tf-project
```

The CLI is exposed as both `tf-project` and the shorter alias `tfp`.

## Configure

```sh
cd path/to/your-terraform-repo
tfp self init
```

This drops a `tf_project.toml` at the repo root, or — if a `pyproject.toml`
is already present — appends a `[tool.tf_project]` section to it. It refuses
to overwrite an existing config.

You can also write the file yourself:

```toml
[tf_project]
terraform_dir    = "terraform"            # where your <project>/ subdirs live
tfvars_dir       = "tfvars"               # scanned for slots; used by `tfp fmt`
tmp_dir          = "tmp"                  # slot dirs + plugin cache land here
state_key_prefix = "terraform/azure/"     # remote backend key prefix

# Optional. Defaults to `shutil.which("terraform")` at config-load time.
# terraform_binary = "bin/terraform-1.7.5"

# Optional. Defaults to `<tmp_dir>/plugin-cache`. Set to "" to disable.
# plugin_cache_dir = "tmp/plugin-cache"

# Optional. Static `-backend-config` k/v pairs applied to every slot init.
[tf_project.backend_config]
# resource_group_name  = "tfstate-rg"
# storage_account_name = "tfstate0001"
# container_name       = "tfstate"

# Optional. Defaults to `op inject`. Set `command = []` to disable.
[tf_project.secrets]
command = ["op", "inject", "--in-file", "{in}", "--out-file", "{out}"]
```

Unknown fields in `tf_project.toml` are rejected at load time.

### Tfvars banner

Each tfvars file carries a one-line JSON banner that identifies its project
and (optionally) names its slot:

```hcl
# {"header": "terraform", "project": "demo"}
foo = "bar"
```

The slot name defaults to the tfvars filename stem (`tfvars/dev.tfvars` →
slot `dev`). Override with a `slot` field:

```hcl
# {"header": "terraform", "project": "demo", "slot": "production"}
```

Banner fields:

| Field            | Purpose                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `header`         | Must be `"terraform"`. Identifies the comment line as a tf-project banner.                                                       |
| `project`        | Subdirectory under `terraform_dir/` containing the source `.tf` files.                                                            |
| `slot`           | Slot name. Defaults to the tfvars filename stem. Must be unique across all tfvars in `tfvars_dir`.                                |
| `state_key`      | Full remote-state backend key. Overrides the default `<state_key_prefix><tfvars-stem>.tfstate`. Use to share state across files.  |
| `env`            | JSON object of `string → string` env vars passed to terraform when running against this slot.                                     |
| `backend_config` | JSON object of extra `-backend-config k=v` pairs. Wins over the config-level `[tf_project.backend_config]` table.                |

Unknown banner fields are tolerated (forward-compatible across tfp versions)
but logged to stderr — so a typo doesn't disappear silently.

## Usage

```sh
tfp use dev                     # switch to slot "dev" (no init if already warm)
tfp use tfvars/prod.tfvars      # path also accepted (useful with shell completion)
tfp ls                          # list slots; active marked with `*`
tfp rm staging                  # remove a slot's on-disk directory

tfp plan                        # plan using the active slot
tfp plan -t module.foo.bar      # targeted plan (repeatable)
tfp plan -r module.foo.bar      # force-replace (repeatable)
tfp apply                       # apply the saved plan
tfp refresh                     # apply directly (no saved plan)
tfp destroy -t module.foo.bar   # targeted destroy
tfp fmt                         # terraform fmt -recursive over terraform/ + tfvars/
tfp output                      # terraform output -json
tfp state list                  # list resources in state
tfp state show aws_x.foo
tfp state mv  aws_x.a aws_x.b
tfp state rm  aws_x.foo aws_x.bar
tfp state pull                  # tfstate JSON to stdout
tfp state push backup.tfstate
tfp state replace-provider hashicorp/aws registry.acme.local/aws
tfp state identities            # 1.10+
tfp import aws_s3_bucket.foo my-bucket
                                # forwards the decrypted tfvars + saved env
tfp status                      # one-line summary of the active slot
tfp last                        # last terraform invocation for the active slot
```

### Choosing the active slot

Three precedence levels — the most specific wins:

| Source             | Wins over    | Lifetime                                  |
| ------------------ | ------------ | ----------------------------------------- |
| `-s/--slot <name>` | everything   | single invocation only (does NOT save)    |
| `TFP_SLOT=<name>`  | saved file   | current shell                             |
| `tmp/active` file  | —            | persistent until next `tfp use`           |

```sh
tfp -s prod plan                # one-off, doesn't touch the saved active
TFP_SLOT=prod tfp plan          # current shell only
tfp use prod                    # writes tmp/active (affects all shells without TFP_SLOT)
```

### Global flags

- `--verbose` — echo the terraform argv to stderr before exec.
- `--dry-run` — print the argv and skip execution.
- `-s/--slot <name>` — operate against a specific slot for one invocation.

```sh
tfp --dry-run plan -t module.foo
tfp -s prod --verbose apply
```

### Plugin cache and the lock file

`tfp` enables Terraform's shared plugin cache by default at
`<tmp_dir>/plugin-cache/`. Providers download once and link into every
slot's `.terraform/providers/`, so the second slot's first init is seconds.

The trade-off: `TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1` is set so
Terraform tolerates the cache's missing per-platform hashes. The first time
your code is init'd on a new platform, the `.terraform.lock.hcl` will only
record that platform's hashes. To pre-populate hashes for CI platforms you
don't run on locally, run:

```sh
tfp self providers lock                                    # default: linux/darwin × amd64/arm64
tfp self providers lock -p linux_amd64 -p windows_amd64    # custom platform set
```

Disable the cache by setting `plugin_cache_dir = ""` in the config.

### Recovering from a stuck Azure / S3 tfstate lock

When terraform is killed mid-operation, the remote backend can leave a
stale lock on the tfstate. Subsequent runs fail with "state locked".

```sh
tfp self lock status            # lease + lock metadata (exits 2 if locked)
tfp self lock break             # break the lock (prompts for confirmation; -y to skip)
tfp force-unlock <LOCK_ID>      # backend-agnostic, via terraform passthrough
```

`tfp self lock` auto-detects the backend from the active slot's saved state:

- **azurerm** — `storage_account_name` + `container_name` present. Shells out
  to `az storage blob`.
- **s3** — `bucket` + `dynamodb_table` present. Shells out to `aws dynamodb`.

Both need the respective CLI installed and authenticated.

`tfp self lock break` is **polite by default**: it discovers the lock ID and
runs `terraform force-unlock <ID>` first, falling back to a backend-level
lease break only if the polite path fails. Pass `--blunt` to skip terraform
entirely.

### Apply safety

`tfp plan` records a SHA-256 of the decrypted tfvars + every `.tf` under
`source_root` in `slots/<slot>/tfplan.meta.json`, alongside the active
banner's `project`, `state_key`, `backend_config`, and `env`. `tfp apply`
refuses to run if any of those drift between plan and apply. Pass
`--force` to override.

### Passthrough to `terraform`

Any subcommand not in the wrapped list above is forwarded to `terraform`
verbatim, prefixed with `-chdir=<source_root>` and the env from the active
slot's saved state:

```sh
tfp validate                          # terraform -chdir=... validate
tfp workspace list                    # per-slot workspace state via TF_DATA_DIR
tfp providers schema -json
tfp version                           # works without an active slot
```

Wrapped subcommands also accept extra terraform flags, which are appended
to the underlying invocation:

```sh
tfp plan -t module.foo -- -detailed-exitcode -compact-warnings
tfp apply -- -parallelism=20
```

### Self-management

```sh
tfp self init                       # bootstrap tf_project.toml or [tool.tf_project]
tfp self config print               # show effective config (--json for JSON)
tfp self config path                # show which file the config came from
tfp self state show                 # active slot's saved state as JSON
tfp self state show --all           # every slot's state
tfp self state clear                # delete the active slot's saved state
tfp self state clear --all          # nuke every slot's saved state
tfp self doctor                     # sanity-check the environment (PATH, dirs, slot uniqueness, orphans)
tfp self banner check <tfvars>      # validate a banner; print resolved slot + fields
tfp self snapshot                   # `terraform state pull` to <tmp_dir>/snapshot-<ts>.tfstate
tfp self trace <subcommand>         # print the argv tfp would build; no exec
tfp self lock status                # remote-state lock state (azurerm or s3+dynamodb)
tfp self lock break                 # polite-by-default release; --blunt skips terraform
tfp self providers lock             # cross-platform `terraform providers lock`
tfp force-unlock <LOCK_ID>          # backend-agnostic, via terraform passthrough
```

## Migrating from a pre-slot install

The first time you run any `tfp` command after upgrading, tf-project detects
the legacy `tmp/my_terraform_state.json` and promotes it into the new layout
under `tmp/slots/<slot>/`, sets `tmp/active`, and leaves a one-line
`tmp/MIGRATED-<UTC-timestamp>.txt` breadcrumb. The first command afterwards
will repopulate the slot's `.terraform/` from the plugin cache.

If the legacy state references a tfvars that has since moved or been deleted,
migration logs a warning, deletes the legacy file, and asks you to run
`tfp use <slot>` to start fresh.

## Development

```sh
pdm install
pdm run ruff check src tests
pdm run pyright src
pdm run pytest                  # unit tests
pdm run pytest -m integration   # smoke tests (need `terraform` on PATH)
```

## Release

Releases are **tag-driven**. The wheel's version comes from the git tag,
not a code edit — there is no `__version__.py` to bump.

1. Move the `## [Unreleased]` block in `CHANGELOG.md` under a new
   `## [X.Y.Z] — YYYY-MM-DD` heading. Commit to `main`.
2. Tag the commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The **Release** workflow fires on the tag push:
   - Re-runs CI against the tagged commit.
   - Builds sdist + wheel with the version pinned from the tag.
   - Signs both with [Sigstore][sigstore] (keyless, transparency-logged).
   - Publishes to TestPyPI and then PyPI via OIDC trusted publishing.
   - Creates a GitHub Release with the extracted changelog section and the
     signed artifacts (`.whl`, `.tar.gz`, `.sigstore`) attached.

Tags carrying `rc` / `a` / `b` / `dev` suffixes (e.g. `v1.0.0rc1`) are
auto-marked as pre-releases on GitHub.

PyPI trusted-publishing is a one-time setup on
`pypi.org → Manage project → Publishing`, tied to this repo and the
`release` GitHub Environment.

[sigstore]: https://www.sigstore.dev/
