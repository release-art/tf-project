# Configuration

tf-project reads two kinds of settings:

- **`tf_project.toml`**, once per repository: where things live and how tfp
  behaves.
- **The banner**, once per tfvars file: which project the file belongs to and
  anything specific to that environment.

## `tf_project.toml`

tfp looks for `tf_project.toml` in the current directory and its parents. If
there is none, it uses the `[tool.tf_project]` table of the nearest
`pyproject.toml`. `tfp self config path` shows which file it found, and
`tfp self config print` shows the settings in effect.

Every setting is optional. Relative paths are relative to the file's
directory.

```toml
[tf_project]
terraform_dir    = "terraform"          # one subdirectory per Terraform project
tfvars_dir       = "tfvars"             # searched (recursively) for *.tfvars
tmp_dir          = "tmp"                # tfp's working files; add it to .gitignore
state_key_prefix = "terraform/azure/"   # see "Backend settings" below
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `terraform_dir` | `terraform` | Where the Terraform projects are, one subdirectory each. |
| `tfvars_dir` | `tfvars` | Where the tfvars files are. Subdirectories are searched too. |
| `tmp_dir` | `tmp` | Slot directories, saved plans, snapshots and the provider cache. |
| `state_key_prefix` | none | Prefix for a backend `key` made from the tfvars file name. |
| `terraform_binary` | `terraform` on `PATH` | The program to run: a name (`tofu`) looked up on `PATH`, or a path. |
| `backend_config` | none | A table of `-backend-config` values for every slot. |
| `plugin_cache_dir` | `<tmp_dir>/plugin-cache` | Shared provider cache. `""` turns it off. |
| `extra_lock_platforms` | none | More platforms for the provider lock file, e.g. `["windows_amd64"]`. |
| `snapshot_retention` | `200` | State snapshots kept per slot. `0` turns automatic snapshots off. |
| `programs.op` | `op` on `PATH` | The 1Password CLI, for `op://` secrets. |
| `programs.az` | `az` on `PATH` | The Azure CLI, for `az-kv://` secrets and azurerm lock commands. |
| `programs.aws` | `aws` on `PATH` | The AWS CLI, for S3 lock commands. |

The `programs` settings go in their own table, for tools that aren't on
`PATH`; like `terraform_binary`, each takes a name or a path:

```toml
[tf_project.programs]
op = "/opt/1password/op"
az = "C:/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az.cmd"
```

A misspelt or unknown setting is an error, so typos don't go unnoticed.

The provider cache, lock-file platforms and snapshots are explained in
[Safety and recovery](safety.md).

## The banner

Each tfvars file starts with a comment holding a small JSON object, the
*banner*:

```hcl
# {"header": "terraform", "project": "api"}
instance_count = 2
```

| Field | Required | Meaning |
| --- | --- | --- |
| `header` | yes | Always `"terraform"`; it marks the line as a banner. |
| `project` | yes | The Terraform project: a subdirectory of `terraform_dir`. |
| `slot` | no | The slot name. Defaults to the file name without `.tfvars`. |
| `state_key` | no | The backend `key` for this environment's state (see below). |
| `backend_config` | no | More `-backend-config` values for this environment. |
| `env` | no | Environment variables for Terraform, e.g. `ARM_SUBSCRIPTION_ID`. |
| `secrets_config` | no | Settings for secret sources; see [Secrets](secrets.md). |

Unknown fields don't stop anything, so older and newer versions of tfp can
share files. `tfp self banner check <file>` lists any it doesn't recognise,
and shows what the banner resolves to.

### Slot names

A slot is one environment: one tfvars file, with its own `.terraform/`
directory under `tmp_dir`. Its name is the banner's `slot`, or else the file
name: `tfvars/prod.tfvars` is slot `prod`.

Names must be unique across `tfvars_dir`. If two files claim the same name,
`tfp use` refuses and tells you which files clash; give one of them a `slot`.

### Backend settings

When tfp initialises a slot, it passes `-backend-config` values gathered from
these places, later ones winning:

1. `key = "<state_key_prefix><file name>.tfstate"`, if `state_key_prefix` is
   set.
2. `backend_config` in `tf_project.toml`.
3. `backend_config` in the banner.
4. The banner's `state_key`, as `key`.

`state_key` must be a clean absolute path such as `/shared/network.tfstate`:
it starts with `/` and has no `.`, `..`, `//`, trailing `/` or backslashes. tfp
drops the leading `/` before passing it on, because the azurerm backend adds
the container name in front. Use `state_key` when two tfvars files should
share one state, or when the default key doesn't fit.

After editing a banner's `state_key` or `backend_config`, run `tfp init` to
re-bind the slot to the new backend.

## Files under `tmp_dir`

You don't normally need to look in here, but when something looks wrong:

```text
tmp/
  active                  the slot `tfp use` last chose
  plugin-cache/           shared provider downloads
  snapshots/<slot>/       remote state snapshots
  slots/<slot>/
    .terraform/           the slot's Terraform working directory
    state.json            what `tfp use` saved about the slot
    backend.hash          fingerprint of the backend it was initialised with
    tfplan, tfplan.meta.json   the saved plan and its safety record
    last.json             the last Terraform command (`tfp last`)
```

`tfp ls` and `tfp self doctor` point out slot directories that no tfvars file
claims any more; `tfp rm <slot>` removes one.
