# tf-project

[![PyPI](https://img.shields.io/pypi/v/tf-project.svg)](https://pypi.org/project/tf-project/)
[![License](https://img.shields.io/github/license/release-art/tf-project.svg)](https://github.com/release-art/tf-project/blob/main/LICENSE)

**tf-project** (`tfp` for short) is a small, opinionated wrapper around
Terraform and OpenTofu for projects with one tfvars file per environment.

You keep `dev.tfvars`, `staging.tfvars` and `prod.tfvars` side by side. Plain
Terraform makes you re-run `terraform init` with the right backend settings
every time you switch between them, and remember which one you're on. With tfp
you switch with `tfp use prod`, and `tfp plan` / `tfp apply` act on that
environment until you switch again.

What you get:

- **Instant switching.** Each environment (a *slot*) keeps its own
  `.terraform/` directory, so switching back to one you've used is just a
  pointer change, with no re-init.
- **Safer applies.** `tfp apply` only applies the plan you just reviewed, and
  refuses if the tfvars, the `.tf` sources or the backend changed since.
- **Protected environments.** Mark `prod` as protected and every command that
  changes it asks you to type its name first, after showing what the plan
  destroys or replaces.
- **A safety net for state surgery.** `tfp state rm`, `mv` and `import`
  snapshot the remote state first, and `tfp self snapshot restore` puts it
  back.
- **Secrets out of git.** Tfvars can hold references like
  `{{ op://Infra/db/password }}` or `{{ az-kv://my-vault/secrets/db-password }}`; tfp
  fills them in from 1Password or Azure Key Vault at run time.
- **Drift checks.** `tfp drift` plans every environment and reports which
  ones no longer match their code.
- **Help when things go wrong.** `tfp self doctor` checks your setup, and
  `tfp self lock break` clears a stuck Azure or S3 state lock.

## Install

```sh
brew install release-art/tap/tf-project   # macOS, Linux
pip install tf-project                     # or: uv tool install tf-project
```

On Windows, with [Scoop](https://scoop.sh):

```powershell
scoop bucket add release-art https://github.com/release-art/scoop-bucket
scoop install release-art/tf-project
```

You get one native program under two names, `tfp` and `tf-project`, for
macOS, Linux and Windows; pip only delivers it, no Python is needed to run it.
[GitHub releases](https://github.com/release-art/tf-project/releases) also
have plain `.tar.gz` / `.zip` downloads.

## A five-minute tour

Say your repository looks like this:

```text
terraform/
  api/            main.tf, variables.tf, …
tfvars/
  dev.tfvars
  prod.tfvars
```

**1. Create a config** at the repository root:

```sh
tfp self init
```

This writes a `tf_project.toml` with sensible defaults (or adds a
`[tool.tf_project]` section to an existing `pyproject.toml`).

**2. Tell each tfvars file which project it belongs to**, with a one-line
JSON comment at the top, the *banner*:

```hcl
# {"header": "terraform", "project": "api"}
instance_count = 2
```

The slot is named after the file: `tfvars/dev.tfvars` becomes slot `dev`.

**3. Switch to an environment and work in it:**

```sh
tfp use dev       # first time: runs terraform init for this slot
tfp plan          # plans against dev, saves the plan
tfp apply         # applies exactly that plan
tfp use prod      # switch; dev stays initialised for next time
tfp status        # which slot am I on?
```

If anything looks off, `tfp self doctor` checks your setup.

## Everyday commands

| Command | What it does |
| --- | --- |
| `tfp use <slot>` | Switch to a slot (by name, or by tfvars path). |
| `tfp ls` | List slots; the active one is marked `*`. |
| `tfp status` | Show the active slot and whether a plan is waiting. |
| `tfp plan` / `tfp apply` | Plan and save; apply the saved plan. `-t <address>` targets. |
| `tfp state …` / `tfp import …` | Terraform's state commands, with a snapshot first. |
| `tfp drift` | Plan every slot without saving, and report which ones drifted. |
| `tfp tf <anything>` | Any other Terraform command, run in the active slot. |
| `tfp -s prod plan` | Run one command against another slot without switching. |
| `tfp --dry-run …` | Show the Terraform command line instead of running it. |

## Learn more

- [Configuration](https://github.com/release-art/tf-project/blob/main/docs/configuration.md):
  `tf_project.toml`, the tfvars banner, slot names, backend settings and the
  provider cache.
- [Commands](https://github.com/release-art/tf-project/blob/main/docs/commands.md):
  every command and flag, and how tfp picks the active slot.
- [Secrets](https://github.com/release-art/tf-project/blob/main/docs/secrets.md):
  1Password and Azure Key Vault references in tfvars.
- [Safety and recovery](https://github.com/release-art/tf-project/blob/main/docs/safety.md):
  stale-plan checks, protected slots, state snapshots, the Azure sign-in check
  and stuck locks.
- [Development](https://github.com/release-art/tf-project/blob/main/docs/development.md):
  building, testing and releasing tf-project itself.
- [Changelog](https://github.com/release-art/tf-project/blob/main/CHANGELOG.md)

## License

MIT
