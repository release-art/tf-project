# tf-project

[![PyPI](https://img.shields.io/pypi/v/tf-project.svg)](https://pypi.org/project/tf-project/)
[![License](https://img.shields.io/github/license/release-art/tf-project.svg)](https://github.com/release-art/tf-project/blob/main/LICENSE)

A thin, opinionated **Terraform / OpenTofu** wrapper for multi-environment
projects. Provides a single `tf-project` (aka `tfp`) CLI built around
**named slots** - each tfvars file binds to a slot, and switching between
slots is a pointer write, not a re-`terraform init`.

If you maintain one tfvars per environment (`dev.tfvars`, `staging.tfvars`,
`prod.tfvars`, …) and you're tired of re-initialising the backend every
time you switch, this tool replaces that ritual with `tfp use dev`.

- **Slots, not paths.** Banner-declared (or filename-derived) names refer to
  tfvars files. `tfp use dev` flips the active slot; `tfp plan` / `tfp apply`
  operate against it.
- **One `.terraform/` per slot** via `TF_DATA_DIR`. Two slots pointing at the
  same project keep independent provider/module/backend bindings on disk -
  swapping between them is instant when both are warm.
- **Shared provider cache** via `TF_PLUGIN_CACHE_DIR`. Providers download
  once and link into every slot's `.terraform/providers/`.
- **Versioned, strictly validated state files.** Every persisted JSON carries
  `schema_version`; unknown fields fail loudly (except in user-authored
  banners, where they are tolerated and reported by `tfp self banner check`).
- **Secrets stay out of git.** Tfvars hold references like
  `{{ op://vault/item/field }}`; tfp fills them in from [1Password][op] at
  run time, into a private copy that is shredded afterwards.

[op]: https://developer.1password.com/docs/cli/

## Install

```sh
pip install tf-project      # or: uv tool install tf-project / pipx install tf-project
```

The CLI is exposed as both `tf-project` and the shorter alias `tfp`. It is a
single native program; the PyPI wheels (Linux x86_64 / arm64, macOS arm64,
Windows x86_64) contain nothing else.

To install without Python, download
`tf-project-<version>-<target>.tar.gz` (`.zip` for Windows) from a
[GitHub Release][releases] and put `tfp` / `tf-project` on your `PATH`. Every
release file is signed with [Sigstore][sigstore].

[releases]: https://github.com/release-art/tf-project/releases

## Configure

```sh
cd path/to/your-terraform-repo
tfp self init
```

This drops a `tf_project.toml` at the repo root, or - if a `pyproject.toml`
is already present - appends a `[tool.tf_project]` section to it. It refuses
to overwrite an existing config.

You can also write the file yourself:

```toml
[tf_project]
terraform_dir    = "terraform"            # where your <project>/ subdirs live
tfvars_dir       = "tfvars"               # scanned for slots; used by `tfp fmt`
tmp_dir          = "tmp"                  # slot dirs + plugin cache land here
state_key_prefix = "terraform/azure/"     # remote backend key prefix

# Optional. Defaults to `terraform` as found on PATH. A bare name (`tofu`) is
# looked up on PATH when run; a path is taken relative to the project root.
# terraform_binary = "bin/terraform-1.7.5"

# Optional. Defaults to `<tmp_dir>/plugin-cache`. Set to "" to disable.
# plugin_cache_dir = "tmp/plugin-cache"

# Optional. Static `-backend-config` k/v pairs applied to every slot init.
[tf_project.backend_config]
# resource_group_name  = "tfstate-rg"
# storage_account_name = "tfstate0001"
# container_name       = "tfstate"
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
| `state_key`      | Full remote-state backend key. Overrides the default `<state_key_prefix><tfvars-stem>.tfstate`. Must be an absolute, normalised pure POSIX path (`/shared/global.tfstate`); relative keys, `.`/`..`, `//`, trailing `/`, and backslashes are rejected. The single leading slash is stripped before use (azurerm prepends the container, so a leading slash would double it). Use to share state across files. |
| `env`            | JSON object of `string → string` env vars passed to terraform when running against this slot.                                     |
| `backend_config` | JSON object of extra `-backend-config k=v` pairs. Wins over the config-level `[tf_project.backend_config]` table.                |

Unknown banner fields are tolerated (forward-compatible across tfp versions);
`tfp self banner check` names them, so a typo doesn't disappear silently.

### Secrets in tfvars

Write a secret as a reference in double braces, wherever its value goes: a
whole value, or part of a longer string, including multi-line strings and
heredocs. Spaces inside the braces are optional.

```hcl
# {"header": "terraform", "project": "demo"}
db_password = "{{ op://Infra/postgres-prod/password }}"

app_config = <<EOT
listen: 8080
api_key: {{op://Infra/api/credential}}
greeting: {{ .Values.greeting }}
EOT
```

Only `{{ scheme://… }}` is treated as a reference; other `{{ … }}` (such as
the Go-template line above) is left as written.

Before running Terraform, tfp collects the file's references, groups them by
scheme, and fetches each group from its source in one go. It writes the
filled-in file to a private temporary directory, passes that to Terraform as
`-var-file`, and overwrites it with zeros and deletes it afterwards. A tfvars
file without references is used as it is.

| Scheme   | Source                                                                                     |
| -------- | ------------------------------------------------------------------------------------------ |
| `op://`  | 1Password, through one `op inject` call. Needs the [1Password CLI][op], signed in through the desktop app or with `OP_SERVICE_ACCOUNT_TOKEN`. |

A reference with any other scheme stops the command with an error naming the
file and the scheme. `tfp self doctor` lists the schemes your tfvars use and
whether each one's source is available. Values are inserted exactly as
returned, so a value containing `"` or `\` needs escaping in the secret store
for HCL.

## Usage

```sh
tfp use dev                     # switch to slot "dev" (no init if already warm)
tfp use tfvars/prod.tfvars      # a tfvars path works too
tfp init                        # re-bind the active slot's backend from its banner
                                # (run after editing a banner's state_key/backend_config)
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
tfp status                      # summary of the active slot
tfp last                        # last terraform invocation for the active slot
```

### Choosing the active slot

Three precedence levels - the most specific wins:

| Source             | Wins over    | Lifetime                                  |
| ------------------ | ------------ | ----------------------------------------- |
| `-s/--slot <name>` | everything   | single invocation only (does NOT save)    |
| `TFP_SLOT=<name>`  | saved file   | current shell                             |
| `tmp/active` file  | -            | persistent until next `tfp use`           |

```sh
tfp -s prod plan                # one-off, doesn't touch the saved active
TFP_SLOT=prod tfp plan          # current shell only
tfp use prod                    # writes tmp/active (affects all shells without TFP_SLOT)
```

### Global flags

- `--verbose` - echo the terraform argv to stderr before running it.
- `--dry-run` - print the argv and run nothing.
- `-s/--slot <name>` - operate against a specific slot for one invocation.

`--verbose` and `--dry-run` are accepted anywhere before a `--`; `-s` goes
before the subcommand.

```sh
tfp --dry-run plan -t module.foo
tfp -s prod --verbose apply
```

### Plugin cache and the lock file

`tfp` enables Terraform's shared plugin cache by default at
`<tmp_dir>/plugin-cache/`. Providers download once and link into every
slot's `.terraform/providers/`, so the second slot's first init is seconds.

The trade-off: `TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE=1` is set so
Terraform tolerates the cache's missing per-platform hashes. To keep
`.terraform.lock.hcl` portable across machines, every `tfp use` that runs
`terraform init` chases it with `terraform providers lock` for
`linux_amd64` + `linux_arm64` (the common CI / runtime targets) plus the
host's own platform - so on an Apple Silicon Mac you get three sets of
hashes (the two Linux ones + `darwin_arm64`), on Linux you just get the
two Linux ones, etc.

Teams that span multiple host platforms can extend the auto-lock set via
`tf_project.toml` so everyone's hashes land in the same lockfile regardless
of who ran `tfp use` last:

```toml
[tf_project]
# linux_amd64, linux_arm64, and the host's own platform are always included;
# anything listed here is added on top.
extra_lock_platforms = ["windows_amd64", "darwin_amd64"]
```

To re-lock against a custom platform set (e.g. add `windows_amd64`):

```sh
tfp self providers lock -p linux_amd64 -p windows_amd64
```

Disable the cache by setting `plugin_cache_dir = ""` in the config.

### Recovering from a stuck Azure / S3 tfstate lock

When terraform is killed mid-operation, the remote backend can leave a
stale lock on the tfstate. Subsequent runs fail with "state locked".

```sh
tfp self lock status            # lease + lock metadata (exits 2 if locked)
tfp self lock break             # break the lock (prompts for confirmation; -y to skip)
tfp tf force-unlock <LOCK_ID>   # backend-agnostic, via `tfp tf`
```

`tfp self lock` auto-detects the backend from the active slot's saved state:

- **azurerm** - `storage_account_name` + `container_name` present. Shells out
  to `az storage blob`.
- **s3** - `bucket` + `dynamodb_table` present. Shells out to `aws dynamodb`.

Both need the respective CLI installed and authenticated.

`tfp self lock break` is **polite by default**: it discovers the lock ID and
runs `terraform force-unlock <ID>` first, falling back to a backend-level
lease break only if the polite path fails. Pass `--blunt` to skip terraform
entirely.

### Auto-snapshot before state-mutating ops

`tfp state rm`, `tfp state mv`, `tfp state replace-provider`, and `tfp
import` each run `terraform state pull` first and save the result to
`<tmp_dir>/snapshots/<slot>/<ts>-pre-<op>.tfstate` before the mutating
command runs. If the pull fails (auth blip, network) the whole op aborts -
no point destroying state you can't roll back to. Recovery:

```sh
tfp self snapshot list
tfp self snapshot restore               # newest, with confirmation
tfp self snapshot restore <name> -y     # specific snapshot, no prompt
```

Restoration uses `terraform state push -force`, which overwrites the
remote state regardless of serial - by definition you're undoing a change,
so the serial check is exactly what you want to skip.

The retention budget is `snapshot_retention` in `tf_project.toml` (default
**200** per slot, pruned newest-first on every write). Set it to `0` to
disable auto-snapshotting entirely; `tfp self snapshot create` still works
either way.

### Apply safety

`tfp plan` records a SHA-256 of the decrypted tfvars + every `.tf` under
`source_root` in `slots/<slot>/tfplan.meta.json`, alongside the active
banner's `project`, `state_key`, `backend_config`, and `env`. `tfp apply`
refuses to run if any of those drift between plan and apply. Pass
`--force` to override.

### Any other Terraform command: `tfp tf`

`tfp tf ...` runs any Terraform command verbatim in the active slot, prefixed
with `-chdir=<source_root>` and the env from the slot's saved state. With no
active slot it runs Terraform as is:

```sh
tfp tf validate                       # terraform -chdir=... validate
tfp tf workspace list                 # per-slot workspace state via TF_DATA_DIR
tfp tf providers schema -json
tfp tf version                        # works without an active slot
```

Only the subcommands listed in this README are wrapped; every other
Terraform command goes through `tfp tf`.

Wrapped subcommands accept Terraform's own flags anywhere after the
subcommand, appended to the underlying invocation; `--` works too:

```sh
tfp plan -t module.foo -detailed-exitcode -compact-warnings
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
tfp self snapshot create            # `terraform state pull` to <tmp_dir>/snapshots/<slot>/<ts>-manual.tfstate
tfp self snapshot list              # list snapshots for the active slot, newest first
tfp self snapshot restore [<name>]  # `terraform state push -force` the named snapshot (default: newest)
tfp self trace <subcommand>         # print the argv tfp would build; runs nothing
tfp self lock status                # remote-state lock state (azurerm or s3+dynamodb)
tfp self lock break                 # polite-by-default release; --blunt skips terraform
tfp self providers lock             # cross-platform `terraform providers lock`
tfp tf force-unlock <LOCK_ID>       # backend-agnostic, via `tfp tf`
```

## Development

The program lives in `rust/`. You need the toolchain pinned in
`rust/rust-toolchain.toml` (rustup installs it on first use) plus
`cargo-nextest`, `cargo-llvm-cov` and `cargo-deny`.

```sh
bin/rust/check.sh               # fmt, clippy, docs, licences, tests, 100% coverage
bin/rust/check.sh --fix         # format and apply clippy's fixes first
bin/run.sh status               # run tfp from the source tree
pip install .                   # build and install the wheel locally (maturin)
```

Tests use fake `terraform` / `az` / `aws` scripts, so they run on macOS and
Linux without any of those installed. Run them with nextest (as check.sh
does): each test gets its own process.

## Release

Releases are **tag-driven**. The wheel's version comes from the git tag,
not a code edit: `rust/Cargo.toml` says `0.0.0`, and the release workflow
writes the tag into it (`bin/rust/set-version.sh`) before building.

1. Move the `## [Unreleased]` block in `CHANGELOG.md` under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading. Commit to `main`.
2. Tag the commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The **Release** workflow fires on the tag push:
   - Re-runs CI against the tagged commit.
   - Builds a wheel per platform (Linux x86_64 + arm64, macOS arm64,
     Windows x86_64) and the sdist, with the version set from the tag.
   - Signs everything with [Sigstore][sigstore] (keyless, transparency-logged).
   - Publishes to PyPI via OIDC trusted publishing.
   - Creates a GitHub Release with the extracted changelog section and the
     signed artifacts attached: the wheels and sdist, plus each platform's
     programs as a standalone `.tar.gz` / `.zip` (`bin/rust/archive.py`).

Tags carrying `rc` / `a` / `b` / `dev` suffixes (e.g. `v1.0.0rc1`) are
auto-marked as pre-releases on GitHub.

PyPI trusted-publishing is a one-time setup on
`pypi.org → Manage project → Publishing`, tied to this repo and the
`release` GitHub Environment.

[sigstore]: https://www.sigstore.dev/
