# Safety and recovery

## Applying only what you reviewed

`tfp plan` saves the plan, along with a record of what it was made from: a
fingerprint of the (filled-in) tfvars and of every `.tf` and `.tf.json` file
under `terraform_dir` (so shared modules there count too), and the banner's `project`, `state_key`, `backend_config` and `env`.

`tfp apply` applies that saved plan, and only if none of those have changed
since. If someone edited a module, the tfvars or the banner in between, it
stops and asks you to plan again. `tfp apply --force` skips the check. After a
successful apply the saved plan is deleted, so it can't be applied twice.

Only one `apply`, `refresh` or `destroy` runs per slot at a time; a second one
stops with "Lock held by another process".

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
