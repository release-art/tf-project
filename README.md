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

| Field       | Purpose                                                                                                                          |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `state_key` | Full remote-state backend key. Overrides the default `<state_key_prefix><tfvars-stem>.tfstate`. Use to share state across files. |
| `env`       | JSON object of `string → string` env vars. Merged into the saved state on top of any previously-captured environment.            |

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
```

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
