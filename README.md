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

Drop a `tf_project.toml` at the root of your Terraform repo:

```toml
[tf_project]
terraform_dir    = "terraform"            # where your <project>/ subdirs live
tfvars_dir       = "tfvars"               # used by `tfp fmt`
tmp_dir          = "tmp"                  # state file + tfplan land here
state_key_prefix = "terraform/azure/"     # remote backend key prefix

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
