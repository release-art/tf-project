# Commands

## Which slot a command uses

Most commands act on the *active slot*. tfp takes the first of:

1. `-s <slot>` / `--slot <slot>` on the command line, for that one command;
2. the `TFP_SLOT` environment variable, for the current shell;
3. the slot `tfp use` last chose, which every shell shares.

```sh
tfp use prod             # prod for every shell from now on
TFP_SLOT=dev tfp plan    # dev in this shell only
tfp -s staging plan      # staging for this one command
```

## Switching and inspecting slots

| Command | What it does |
| --- | --- |
| `tfp use <slot>` | Make `<slot>` active. A tfvars path works too. Runs `terraform init` only when the slot has never been initialised or its backend settings changed. |
| `tfp init` | Re-read the active slot's banner and re-initialise its backend (`terraform init -reconfigure`). Run it after editing `state_key` or `backend_config`. |
| `tfp ls` | List slots, their tfvars file and backend key, and when each was last initialised; protected slots are marked. `--json` for scripts. |
| `tfp status` | The active slot, its files, backend key, environment variables, whether a plan is waiting and whether the slot is protected. |
| `tfp last` | The last Terraform command run in the active slot, and its exit code. |
| `tfp rm <slot>` | Delete a slot's working directory (not its tfvars file). The active slot needs `--force`. |

## Running Terraform

| Command | What it does |
| --- | --- |
| `tfp plan` | Plan, and save the plan for `tfp apply`. `-t <address>` targets and `-r <address>` forces replacement; both repeat. |
| `tfp apply` | Apply the saved plan, if nothing has changed since; `--force` skips that check. See [Safety and recovery](safety.md). |
| `tfp refresh` | Apply without a saved plan, optionally with `-t`. |
| `tfp destroy` | Destroy, optionally with `-t`. |
| `tfp output` | `terraform output -json`. |
| `tfp import <address> <id>` | Import a resource, after snapshotting the remote state. |
| `tfp state list \| show \| mv \| rm \| pull \| push \| replace-provider \| identities` | Terraform's state commands. `mv`, `rm` and `replace-provider` snapshot the remote state first. |
| `tfp drift` | Check every slot for drift; see [below](#checking-every-slot-for-drift). `--json` for scripts. |
| `tfp fmt` | `terraform fmt -recursive` over the Terraform and tfvars directories. |
| `tfp tf <args>` | Any other Terraform command, run in the active slot: `tfp tf validate`, `tfp tf providers`, `tfp tf force-unlock <id>`. Without an active slot, it runs Terraform as is. |

Commands that need the tfvars (`plan`, `apply`, `refresh`, `destroy`,
`import`) get them with secret references filled in; see
[Secrets](secrets.md).

Terraform's own flags can follow the command in any order, or after `--`:

```sh
tfp plan -t module.db -lock=false
tfp apply -- -parallelism=20
```

Only the commands above are wrapped. A Terraform command tfp doesn't know,
like `tfp validate`, is an error rather than a guess: use `tfp tf validate`.

## Checking every slot for drift

`tfp drift` runs `terraform plan -detailed-exitcode -lock=false -input=false`
in each slot in turn, with its own tfvars (secrets filled in), environment and
`.terraform/`, then prints a summary:

```text
SLOT  RESULT
dev   clean
prod  skipped: protected
qa    drift
```

It saves no plan, so each slot's saved plan for `tfp apply` stays as it was,
and it doesn't change the active slot. Protected slots are skipped, and so
are slots that were never initialised (run `tfp use <slot>` first; drift
doesn't initialise anything).

It exits 0 when every slot it checked is clean, 2 when any drifted, and 1
when any check failed. Skipped slots don't count. `--json` prints the summary
as a list of `{"slot", "result", "reason"}` objects, `result` being `clean`,
`drift`, `error` or `skipped`; Terraform's own output then goes to stderr.

## Flags for any command

| Flag | What it does |
| --- | --- |
| `--dry-run` | Print the Terraform command lines and run nothing. Secret references are still resolved. |
| `--verbose` | Print each Terraform command line before running it. |
| `-s <slot>` | Use another slot for this command. Goes before the command name. |
| `--version` | Print the version. |

`--dry-run` and `--verbose` can go anywhere before a `--`.

## Managing tf-project itself: `tfp self`

| Command | What it does |
| --- | --- |
| `tfp self init` | Write a starter `tf_project.toml`, or a `[tool.tf_project]` table in `pyproject.toml`. |
| `tfp self config print` | The settings in effect (`--json` for scripts). |
| `tfp self config path` | Which file the settings come from. |
| `tfp self doctor` | Check Terraform, secret tools, directories, banners, slot names, shared state keys, the Azure sign-in, that git ignores `tmp_dir`, and stray slot directories. |
| `tfp self banner check <tfvars>` | Validate a banner and show what it resolves to. |
| `tfp self trace <command>` | The Terraform command line `tfp <command>` would run, without running it. |
| `tfp self state show` / `clear` | Show or delete what `tfp use` saved about the active slot (`--all` for every slot). |
| `tfp self snapshot create \| list \| restore` | Remote state snapshots; see [Safety and recovery](safety.md). |
| `tfp self lock status \| break` | Inspect or clear a stuck remote state lock; see [Safety and recovery](safety.md). |
| `tfp self providers lock` | Refresh the provider lock file for every platform your team uses. |

## Exit codes

tfp exits with Terraform's exit code when Terraform fails. Its own errors exit
with 1, and a usage mistake with 2. `tfp self lock status` exits with 2 while
the lock is held, so scripts can wait on it.
