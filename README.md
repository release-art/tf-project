# tf-project

A thin, opinionated Terraform-project wrapper. Provides a single `tf-project`
(aka `tfp`) CLI that wraps `terraform init / plan / apply / refresh / destroy /
fmt / output / state mv` with:

- Per-tfvars remote-state backend keys (one state per tfvars file).
- Pluggable tfvars preprocessing — defaults to [1Password's `op inject`][op] so
  you can keep `op://...` references in tfvars committed to git.
- A small JSON state file (`tmp/my_terraform_state.json`) capturing which
  tfvars was last init'd, so subsequent `plan` / `apply` need no arguments.

[op]: https://developer.1password.com/docs/cli/secrets-template-syntax/

## Install

```sh
pip install tf-project
```

The CLI is exposed as both `tf-project` and the shorter alias `tfp`.

## Configure

The fastest way to get a config is:

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
tfvars_dir       = "tfvars"               # used by `tfp fmt`
tmp_dir          = "tmp"                  # state file + tfplan land here
state_key_prefix = "terraform/azure/"     # remote backend key prefix

# Optional. Defaults to `shutil.which("terraform")` at config-load time.
# A value with a path separator is resolved relative to the project root;
# a bare name (e.g. `"tofu"`) is left for `subprocess` to PATH-resolve.
# terraform_binary = "bin/terraform-1.7.5"

# Optional. Static `-backend-config` k/v pairs applied to every `tfp init`.
# Banner-level `backend_config` overrides individual keys.
[tf_project.backend_config]
# resource_group_name  = "tfstate-rg"
# storage_account_name = "tfstate0001"
# container_name       = "tfstate"

# Optional. Defaults to `op inject`. Set `command = []` to disable.
[tf_project.secrets]
command = ["op", "inject", "--in-file", "{in}", "--out-file", "{out}"]
```

Alternatively, place the same fields under `[tool.tf_project]` in your
`pyproject.toml`. `tf_project.toml` wins if both are present. The package
walks up from `cwd` to find either file.

Each tfvars file should carry a one-line JSON banner identifying its project:

```hcl
# {"header": "terraform", "project": "demo"}
foo = "bar"
```

`tfp init <tfvars>` reads that banner to pick the `terraform_dir/<project>/`
subdirectory to operate on, and persists a state record so subsequent
commands take no arguments.

The banner also accepts these optional fields:

| Field            | Purpose                                                                                                                          |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `state_key`      | Full remote-state backend key. Overrides the default `<state_key_prefix><tfvars-stem>.tfstate`. Use to share state across files. |
| `env`            | JSON object of `string → string` env vars. Merged into the saved state on top of any previously-captured environment.            |
| `backend_config` | JSON object of extra `-backend-config k=v` pairs (e.g. `resource_group_name`, `storage_account_name`). Wins over the config-level `[tf_project.backend_config]` table. |

```hcl
# {"header":"terraform","project":"core","state_key":"shared/core.tfstate","env":{"ARM_SUBSCRIPTION_ID":"…"}}
foo = "bar"
```

## Usage

```sh
tfp init tfvars/dev.tfvars     # init backend for this tfvars
tfp plan                        # plan using last init'd tfvars
tfp plan -t module.foo.bar      # targeted plan (repeatable)
tfp plan -r module.foo.bar      # force-replace (repeatable)
tfp apply                       # apply the saved plan
tfp refresh                     # apply directly (no saved plan)
tfp destroy -t module.foo.bar   # targeted destroy
tfp fmt                         # terraform fmt -recursive over terraform/ + tfvars/
tfp output                      # terraform output -json
tfp state-mv aws_x.a aws_x.b    # terraform state mv
tfp status                      # one-line summary of the current init
```

### Global flags

- `--verbose` — echo the terraform argv to stderr before exec.
- `--dry-run` — print the argv and skip execution. Combine with any subcommand
  (wrapped or passthrough) to preview what would be invoked.

```sh
tfp --dry-run plan -t module.foo
tfp --verbose apply
```

### Recovering from a stuck Azure tfstate lock

When terraform is killed mid-operation (Ctrl-C, OOM, network drop), the
azurerm backend leaves an infinite blob lease on the tfstate. Subsequent
runs fail with "state locked".

```sh
tfp self lock status            # lease + lock metadata (exits 2 if locked)
tfp self lock break             # break the blob lease (prompts for confirmation; -y to skip)
tfp force-unlock <LOCK_ID>      # backend-agnostic, via terraform passthrough
```

`tfp self lock status` reads the lock ID directly from the blob's
`terraformlockid` metadata (which the azurerm backend writes as
base64-encoded JSON), so you don't have to provoke a failed terraform run
to discover it:

```
locked         = True
lease_state    = leased
lease_duration = infinite
lock_id        = 12345678-90ab-cdef-1234-567890abcdef
lock_who       = user@host
lock_operation = OperationTypePlan
lock_created   = 2026-05-14T12:00:00Z

To release via terraform: tfp force-unlock 12345678-90ab-cdef-1234-567890abcdef
```

`tfp self lock {status,break}` shells out to `az storage blob` and so
requires the Azure CLI to be installed and authenticated. It reads the
storage account / container / blob from the saved init state — so this only
works after `tfp init` has captured `[tf_project.backend_config]`
(`storage_account_name`, `container_name`) and the `key`.

Two options once you have the ID:

- **`tfp force-unlock <ID>`** — the polite version: terraform releases the
  lease *and* deletes the lock metadata. Works for any backend.
- **`tfp self lock break`** — the blunt version: breaks the blob lease
  without going through terraform. Useful when terraform itself can't
  reach the backend or when you don't have the ID.

### Apply safety

`tfp plan` records a SHA-256 of the decrypted tfvars alongside the saved
tfplan (`<tfplan>.meta.json`). `tfp apply` refuses to run if the tfvars
content changed since the plan was generated. Pass `--force` to override.

### Passthrough to `terraform`

Any subcommand not in the wrapped list above is forwarded to `terraform`
verbatim, prefixed with `-chdir=<source_root>` and the environment from the
last `tfp init`. So the full Terraform CLI surface is reachable through `tfp`:

```sh
tfp validate                          # terraform -chdir=... validate
tfp validate -json                    # flags pass straight through
tfp workspace list                    # terraform -chdir=... workspace list
tfp taint module.foo.bar              # terraform -chdir=... taint module.foo.bar
tfp providers schema -json            # terraform -chdir=... providers schema -json
tfp version                           # works without init (no -chdir prepended)
```

The wrapped subcommands also accept extra terraform flags, which are appended
to the underlying invocation:

```sh
tfp plan -t module.foo -- -detailed-exitcode -compact-warnings
tfp apply -- -parallelism=20
```

(The `--` is optional — anything Typer doesn't recognise is forwarded — but
including it is the most readable way to signal "everything after this is raw
terraform flags".)

### Self-management

A group of `tfp self ...` commands manages the tool itself, not Terraform:

```sh
tfp self init                  # bootstrap tf_project.toml or [tool.tf_project]
tfp self config print          # show effective config (--json for JSON)
tfp self config path           # show which file the config came from
tfp self state show            # pretty-print the saved init state
tfp self state clear           # delete the saved state file
tfp self doctor                # sanity-check the environment (PATH, dirs, ...)
tfp self banner check <tfvars> # validate a tfvars banner; print resolved fields
tfp self lock status           # show Azure blob-lease state of the remote tfstate
tfp self lock break            # break the lease after a hard-kill left state locked
tfp force-unlock <LOCK_ID>     # backend-agnostic, via terraform passthrough
```

## Development

```sh
pdm install
pdm run ruff check src tests
pdm run pyright src
pdm run pytest                  # unit tests
pdm run pytest -m integration   # smoke tests (need `terraform` on PATH)
```

## Release

1. Merge to `main`. CI runs; on success the **Release** workflow triggers.
2. `Release` builds the wheel, publishes to TestPyPI then PyPI via OIDC
   trusted publishing, then opens a `pdm bump patch` commit to prepare the
   next version.
3. PyPI trusted-publishing setup is a one-time step on
   `pypi.org → Manage project → Publishing`, tied to this repo and the
   `release` GitHub Environment.
