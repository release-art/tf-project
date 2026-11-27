# Safety and recovery

## Applying only what you reviewed

`tfp plan` saves the plan, along with a record of what it was made from: a
fingerprint of the (filled-in) tfvars; of every `.tf`, `.tf.json`, `.tofu`
and `.tofu.json` file under `terraform_dir` (so shared modules there count
too); of the variable files Terraform loads by itself (`terraform.tfvars`,
`terraform.tfvars.json`, `*.auto.tfvars`, `*.auto.tfvars.json`); and the
banner's `project`, `state_key`, `backend_config` and `env`.

`.terraform.lock.hcl` isn't part of it: `tfp use` on another slot of the same
project rewrites it, which would make every other slot's plan look stale, and
Terraform already checks the providers against the saved plan.

`tfp apply` applies that saved plan, and only if none of those have changed
since. If someone edited a module, the tfvars or the banner in between, it
stops and asks you to plan again. `tfp apply --force` skips the check. After a
successful apply the saved plan is deleted, so it can't be applied twice.

Only one `apply`, `refresh` or `destroy` runs per slot at a time; a second one
stops with "Lock held by another process".

## Protected slots

The active slot is shared by every shell, so a `tfp destroy` meant for `dev`
can land on `prod` if another terminal ran `tfp use prod` in between. And
`tfp apply` applies the saved plan without asking: Terraform doesn't prompt
for saved plans. Mark the environments where that would hurt:

```hcl
# {"header": "terraform", "project": "api", "protected": true}
```

Then `tfp apply`, `refresh`, `destroy`, `import`, `state mv`, `state rm`,
`state push`, `state replace-provider` and `tfp self snapshot restore` ask you
to type the slot's name before they run:

```text
slot "prod" is protected. Type its name to run `tfp apply`:
```

For `tfp apply` (with or without `--force`), tfp first reads the saved plan
with `terraform show -json` and says what it will do, naming everything it
destroys or replaces:

```text
slot "prod" is protected. This plan adds 3, changes 1, destroys 1, replaces 1:
  - azurerm_storage_account.logs   (destroy)
  - azurerm_postgresql_flexible_server.db   (replace)
Type its name to run `tfp apply`:
```

If `terraform show` fails, tfp says the summary is unavailable and asks
anyway. Unprotected slots apply as before, with Terraform's own output.

Anything else aborts. `-y` and `--force` don't skip this, and `--dry-run`
doesn't ask, since it runs nothing. Commands that only read (`plan`, `output`,
`state list`, `ls`, `status`, ...) are unchanged. `tfp tf ...` passes
commands to Terraform untouched and isn't guarded.

With no terminal to type at (CI, a pipe), tfp refuses unless `TFP_CONFIRM`
holds the slot's name: `TFP_CONFIRM=prod tfp apply`. A `TFP_CONFIRM` naming
another slot is refused too. At a terminal `TFP_CONFIRM` is ignored, so one
left exported in a shell doesn't silently unprotect it.

`tfp ls`, `tfp status` and `tfp self banner check` show which slots are
protected.

## State snapshots

Some commands change the remote state directly: `tfp state rm`,
`tfp state mv`, `tfp state replace-provider` and `tfp import`. Before running
them, tfp downloads the current remote state (`terraform state pull`) and
keeps a copy under `tmp_dir/snapshots/<slot>/`. If it can't download the
state, it doesn't run the command: there would be nothing to go back to.

```sh
tfp self snapshot list                 # newest first
tfp self snapshot restore              # put the newest one back (asks first)
tfp self snapshot restore <name> -y    # a specific one, without asking
tfp self snapshot create               # take one now, by hand
```

Restoring uses `terraform state push -force`, which overwrites the remote
state even though its serial number is older. That's what you want when
undoing a change, but double-check you're on the right slot first.

tfp keeps the newest 200 snapshots per slot. Change that with
`snapshot_retention` in `tf_project.toml`; `0` turns automatic snapshots off
(`tfp self snapshot create` still works).

## Signed in to the right Azure tenant

Before `apply`, `refresh`, `destroy`, `import`, the state-changing `state`
commands and `tfp self snapshot restore`, tfp asks the Azure CLI who is signed
in (`az account show`) for slots that look like Azure ones: the banner's `env`
sets an `ARM_*` variable, or the backend is azurerm. It only checks; it never
switches accounts.

- If the banner sets `ARM_TENANT_ID` and az is signed in to another tenant,
  tfp stops before running Terraform and tells you which `az login --tenant`
  to run.
- If the banner sets no `ARM_SUBSCRIPTION_ID`, tfp warns that Terraform will
  use az's default subscription, and names it. Pin the subscription in the
  banner's `env` to remove that risk.
- If no one is signed in, tfp stops and asks you to run `az login`.

Nothing is checked for `plan` and read-only commands, with `--dry-run`, or when
`az` isn't installed. `tfp self doctor` runs the same check for every Azure slot
and also reports a missing `az`.

## A stuck state lock

If Terraform is killed halfway through, the backend can be left locked, and
every later command fails with "Error acquiring the state lock".

```sh
tfp self lock status       # who holds the lock, since when (exit code 2 while locked)
tfp self lock break        # release it (asks first; -y skips the question)
```

`tfp self lock break` first tries the gentle way, `terraform force-unlock`
with the lock's ID. Only if that fails does it break the lock at the backend
itself; `--blunt` goes straight there. Make sure nobody is still running
Terraform against that state before you break its lock.

This works for two backends, detected from the slot's backend settings:

- **azurerm** (a blob lease), using the Azure CLI `az`;
- **s3** with DynamoDB locking, using the AWS CLI `aws`.

Both CLIs need to be installed and signed in. If they aren't on `PATH`, set
`[tf_project.programs] az` / `aws` (see [Configuration](configuration.md)).
For any backend, if you know the lock ID, `tfp tf force-unlock <id>` works too.

## Provider downloads and the lock file

All slots share one provider cache (`tmp_dir/plugin-cache`), so a provider is
downloaded once, and initialising a second slot takes seconds.

Terraform's cache can leave the lock file `.terraform.lock.hcl` with checksums
for your platform only, which then fails on CI or a colleague's machine. So
after each `terraform init`, tfp also runs `terraform providers lock` for
Linux amd64 and arm64 plus your own platform. If your team also works on
other platforms, list them in `tf_project.toml`:

```toml
[tf_project]
extra_lock_platforms = ["windows_amd64", "darwin_amd64"]
```

To refresh the lock file by hand, for those platforms or any others:

```sh
tfp self providers lock
tfp self providers lock -p linux_amd64 -p windows_amd64
```

To turn the shared cache off, set `plugin_cache_dir = ""`.
