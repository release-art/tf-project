# Safety and recovery

## Applying only what you reviewed

`tfp plan` saves the plan together with a fingerprint of what it was made
from:

- the tfvars, with secrets filled in;
- every `.tf`, `.tf.json`, `.tofu` and `.tofu.json` file under
  `terraform_dir`, so shared modules count too;
- the variable files Terraform loads by itself (`terraform.tfvars`,
  `*.auto.tfvars`, and their `.json` forms);
- the banner's `project`, `state_key`, `backend_config` and `env`.

`tfp apply` refuses the plan if any of these changed since; run `tfp plan`
again, or `tfp apply --force` to apply it anyway. A successful apply deletes
the saved plan, so it can't be applied twice.

`.terraform.lock.hcl` isn't fingerprinted: `tfp use` on another slot of the
same project may rewrite it, and Terraform checks providers against the saved
plan itself.

Only one `apply`, `refresh` or `destroy` runs per slot at a time.

## Protected slots

The active slot is shared by every shell, so a `tfp destroy` meant for `dev`
lands on `prod` if another terminal ran `tfp use prod` in between. And
Terraform applies a saved plan without asking. Protect the slots where that
would hurt:

```hcl
# {"header": "terraform", "project": "api", "protected": true}
```

Then every command that changes the slot asks you to type its name first:
`apply`, `refresh`, `destroy`, `import`, `state mv | rm | push |
replace-provider` and `self snapshot restore`. `tfp apply` also says what the
plan does:

```text
slot "prod" is protected. This plan adds 3, changes 1, destroys 1, replaces 1:
  - azurerm_storage_account.logs   (destroy)
  - azurerm_postgresql_flexible_server.db   (replace)
Type its name to run `tfp apply`:
```

- `-y` and `--force` don't skip the question; `--dry-run` doesn't ask.
- With no terminal (CI), set `TFP_CONFIRM=<slot>`. At a terminal it's
  ignored, so one left in your shell doesn't unprotect anything.
- Read-only commands and `tfp tf …` aren't guarded.
- `tfp ls` and `tfp status` show which slots are protected.

## State snapshots

`tfp state rm`, `state mv`, `state replace-provider` and `tfp import` change
the remote state directly. Before each, tfp saves a copy of it
(`terraform state pull`) under `tmp_dir/snapshots/<slot>/`, and refuses to run
if it can't.

```sh
tfp self snapshot list                 # newest first
tfp self snapshot restore              # put the newest one back (asks first)
tfp self snapshot restore <name> -y    # a specific one, without asking
tfp self snapshot create               # take one now
```

Restoring runs `terraform state push -force`, overwriting the remote state
even though the snapshot is older. Check you're on the right slot first.

The newest 200 snapshots per slot are kept; set `snapshot_retention` in
`tf_project.toml` to change that, or to `0` to stop automatic snapshots.

## Azure sign-in

Before a command that changes an Azure slot (one whose banner `env` sets an
`ARM_*` variable, or with an azurerm backend), tfp checks `az account show`.
It never switches accounts.

- A banner `ARM_TENANT_ID` other than az's tenant stops the command, telling
  you which `az login --tenant` to run.
- No `ARM_SUBSCRIPTION_ID` in the banner gets a warning naming the
  subscription Terraform will fall back to. Set it to remove the risk.

Nothing is checked for read-only commands, with `--dry-run`, or without `az`
installed. `tfp self doctor` runs the same check for every Azure slot.

## A stuck state lock

If Terraform is killed halfway, the backend can stay locked and every command
fails with "Error acquiring the state lock".

```sh
tfp self lock status       # who holds it, since when (exits 2 while locked)
tfp self lock break        # release it (asks first; -y skips the question)
```

`break` tries `terraform force-unlock` first, and only then breaks the lock at
the backend; `--blunt` goes straight there. Make sure nobody is still running
Terraform on that state.

It works for **azurerm** (a blob lease, via `az`) and **s3 with DynamoDB**
(via `aws`), signed in. For any other backend, `tfp tf force-unlock <id>`.
