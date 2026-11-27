//! `tfp self ...` (and `tfp last`): managing tf-project itself, its saved state, snapshots and the remote lock.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

use serde_json::{Value, json};

use crate::banner::{find_banner, render_summary, unknown_fields_warning};
use crate::commands::{
    Flags, build_apply_argv, build_import_argv, build_init_argv, build_output_argv, build_plan_argv,
    build_providers_lock_argv, build_targeted_argv, confirm_protected, require_active, require_slot_state,
};
use crate::config::{
    CONFIG_FILE_NAME, CONFIG_TABLE, Config, PYPROJECT_FILE_NAME, PYPROJECT_TABLE, locate, parse_toml, walk,
};
use crate::identity;
use crate::lock::{Backend, LockProvider, LockStatus, select_provider};
use crate::models::{LastInvocation, MyState, Versioned};
use crate::state::load_state;
use crate::terraform::{self, env_for_slot};
use crate::text::utc;
use crate::{Ctx, Error, Result, io_error, resolve, say, slots};

/// The body of the `[tf_project]` table `tfp self init` writes.
const DEFAULT_CONFIG_BODY: &str = r#"terraform_dir = "terraform"
tfvars_dir    = "tfvars"
tmp_dir       = "tmp"
state_key_prefix = ""
# terraform_binary = "terraform"       # path or PATH-name; defaults to `which terraform`

# Extra GOOS_GOARCH platforms to include when `tfp use` chases `terraform init`
# with `terraform providers lock`. linux_amd64, linux_arm64, and the host's
# own platform are always included; list anything else your team runs on here.
# extra_lock_platforms = ["windows_amd64", "darwin_amd64"]

# Max number of auto-snapshots kept per slot under <tmp_dir>/snapshots/<slot>/.
# Each `tfp state rm`/`mv`/`replace-provider`/`import` pulls remote state and
# writes a snapshot here before mutating. Set to 0 to disable.
# snapshot_retention = 200

# Where to find the other tools tfp runs, when they aren't on PATH.
# [tf_project.programs]
# op  = "/opt/1password/op"       # 1Password CLI, for {{ op://… }} secrets
# az  = "/opt/az/bin/az"          # Azure CLI, for {{ az-kv://… }} secrets and azurerm locks
# aws = "/usr/local/bin/aws"      # AWS CLI, for S3 locks
"#;

fn render_default_config(table: &str) -> String {
    format!("[{table}]\n{DEFAULT_CONFIG_BODY}")
}

/// `tfp self init`: write a starter config, into `pyproject.toml` when there is one.
///
/// # Errors
/// A config is already there, or the file cannot be written.
pub fn do_self_init(cwd: &Path) -> Result<PathBuf> {
    let standalone = cwd.join(CONFIG_FILE_NAME);
    let pyproject = cwd.join(PYPROJECT_FILE_NAME);
    if standalone.exists() {
        return Err(Error::Msg(format!(
            "{} already exists; not overwriting.",
            standalone.display()
        )));
    }
    if !pyproject.exists() {
        std::fs::write(&standalone, render_default_config(CONFIG_TABLE)).map_err(io_error(&standalone))?;
        return Ok(standalone);
    }
    let table = PYPROJECT_TABLE.join(".");
    let existing = std::fs::read_to_string(&pyproject).map_err(io_error(&pyproject))?;
    if walk(parse_toml(&pyproject, &existing)?, &PYPROJECT_TABLE).is_some() {
        return Err(Error::Msg(format!(
            "[{table}] already exists in {}; not overwriting.",
            pyproject.display()
        )));
    }
    let sep = if existing.ends_with("\n\n") {
        ""
    } else if existing.ends_with('\n') {
        "\n"
    } else {
        "\n\n"
    };
    std::fs::write(&pyproject, format!("{existing}{sep}{}", render_default_config(&table)))
        .map_err(io_error(&pyproject))?;
    Ok(pyproject)
}

/// The effective configuration, with the derived directories.
#[must_use]
pub fn config_json(config: &Config) -> Value {
    let path = |p: &Path| p.display().to_string();
    json!({
        "project_root": path(&config.project_root),
        "terraform_dir": path(&config.terraform_dir),
        "tfvars_dir": path(&config.tfvars_dir),
        "tmp_dir": path(&config.tmp_dir),
        "slots_dir": path(&config.slots_dir()),
        "active_file": path(&config.active_file()),
        "plugin_cache_dir": config.plugin_cache_dir.as_deref().map_or("(disabled)".to_owned(), path),
        "state_key_prefix": config.state_key_prefix,
        "terraform_binary": config.terraform_binary,
        "backend_config": config.backend_config,
        "secret_sources": {"op": "1Password CLI (op inject)"},
        "programs": {"op": config.programs.op, "az": config.programs.az, "aws": config.programs.aws},
        "extra_lock_platforms": config.extra_lock_platforms,
        "snapshot_retention": config.snapshot_retention,
    })
}

/// `tfp self config print`: `key = value` lines, a table's entries indented under it.
#[must_use]
pub fn config_text(config: &Config) -> String {
    let scalar = |v: &Value| v.as_str().map_or_else(|| v.to_string(), str::to_owned);
    let mut lines = Vec::new();
    for (key, value) in config_json(config).as_object().into_iter().flatten() {
        match value.as_object() {
            Some(table) => {
                lines.push(format!("{key}:"));
                lines.extend(table.iter().map(|(k, v)| format!("  {k} = {}", scalar(v))));
            }
            None => lines.push(format!("{key} = {}", scalar(value))),
        }
    }
    lines.join("\n")
}

/// `tfp self config path`: the file the config would be read from.
///
/// # Errors
/// There is none.
pub fn do_self_config_path(cwd: &Path) -> Result<PathBuf> {
    locate(cwd)?.map(|l| l.file).ok_or_else(|| {
        Error::Config(format!(
            "No tf_project config found by walking up from {}. Run `tfp self init` to create one.",
            resolve(cwd).display()
        ))
    })
}

/// `tfp self state show`: the active slot's saved state, or every slot's with `all` (null when never used).
///
/// # Errors
/// No active slot, it has no state, or a state file is unreadable.
pub fn do_self_state_show(config: &Config, ctx: &Ctx, all: bool) -> Result<Value> {
    if all {
        let mut out = serde_json::Map::new();
        for slot in slots::list_slots(config) {
            out.insert(
                slot.name.clone(),
                load_state(config, &slot.name)?
                    .as_ref()
                    .map_or(Value::Null, Versioned::to_json),
            );
        }
        return Ok(Value::Object(out));
    }
    let name = require_active(config, ctx)?.name;
    let state = load_state(config, &name)?
        .ok_or_else(|| Error::Msg(format!("Slot {name:?} isn't initialised. Run `tfp use {name}` first.")))?;
    Ok(state.to_json())
}

/// `tfp self state clear`: delete the active slot's state file, or every slot's with `all`.
///
/// # Errors
/// No active slot, or a file cannot be deleted.
pub fn do_self_state_clear(config: &Config, ctx: &Ctx, all: bool) -> Result<Vec<PathBuf>> {
    let names: Vec<String> = if all {
        slots::list_slots(config).into_iter().map(|s| s.name).collect()
    } else {
        vec![require_active(config, ctx)?.name]
    };
    let mut removed = Vec::new();
    for name in names {
        let path = config.state_file(&name);
        if path.exists() {
            std::fs::remove_file(&path).map_err(io_error(&path))?;
            removed.push(path);
        }
    }
    Ok(removed)
}

/// One `tfp self doctor` line.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DoctorCheck {
    /// What was checked.
    pub name: String,
    /// Whether it passed.
    pub ok: bool,
    /// What was found.
    pub detail: String,
}

fn check(name: impl Into<String>, ok: bool, detail: impl Into<String>) -> DoctorCheck {
    DoctorCheck {
        name: name.into(),
        ok,
        detail: detail.into(),
    }
}

/// Can the secret references in the tfvars be resolved? One check per scheme in use.
fn secret_checks(config: &Config, found: &dyn Fn(&str) -> Option<String>) -> Vec<DoctorCheck> {
    let mut schemes: Vec<String> = slots::list_slots(config)
        .iter()
        .filter_map(|s| std::fs::read_to_string(&s.tfvars).ok())
        .flat_map(|text| crate::secrets::schemes(&text))
        .collect();
    schemes.sort();
    schemes.dedup();
    if schemes.is_empty() {
        return vec![check("secret references", true, "none in the tfvars")];
    }
    schemes
        .into_iter()
        .map(|scheme| {
            let tool = |key: &str, what: &str, program: &str, install: &str| {
                let name = format!("{scheme}:// references: {what} `{program}`");
                match found(program) {
                    Some(path) => check(name, true, path),
                    None => check(
                        name,
                        false,
                        format!("not found; install it ({install}) or set [tf_project.programs] {key}"),
                    ),
                }
            };
            match scheme.as_str() {
                "op" => tool(
                    "op",
                    "1Password CLI",
                    &config.programs.op,
                    "https://developer.1password.com/docs/cli/get-started/",
                ),
                "az-kv" => tool(
                    "az",
                    "Azure CLI",
                    &config.programs.az,
                    "https://learn.microsoft.com/cli/azure/install-azure-cli",
                ),
                other => check(
                    format!("{other}:// references"),
                    false,
                    "no secret source for this scheme",
                ),
            }
        })
        .collect()
}

/// Is `tmp_dir` kept out of git? None when there's no `git`, or `tmp_dir` isn't inside a work tree.
/// Whether az is signed in where every Azure slot expects; none when there is no Azure slot.
fn azure_identity_check(config: &Config, path: Option<&std::ffi::OsStr>) -> Option<DoctorCheck> {
    let azure: Vec<slots::ResolvedSlot> = slots::list_slots(config)
        .into_iter()
        .filter(|s| identity::looks_azure(&s.env, &s.backend_config))
        .collect();
    if azure.is_empty() {
        return None;
    }
    let name = "azure identity";
    let Some(az) = identity::find_az(config, path) else {
        return Some(check(
            name,
            false,
            format!(
                "`{}` not found; install the Azure CLI or set [tf_project.programs] az",
                config.programs.az
            ),
        ));
    };
    let account = match identity::az_account(&az, path.map(ToOwned::to_owned)) {
        Ok(account) => account,
        Err(e) => return Some(check(name, false, e.to_string())),
    };
    let found: Vec<identity::Assessment> = azure
        .iter()
        .map(|s| identity::assess(&s.name, &s.env, &account))
        .collect();
    let errors: Vec<&str> = found.iter().filter_map(|a| a.error.as_deref()).collect();
    let notes = found.iter().filter_map(|a| a.warning.as_deref());
    let detail = std::iter::once(format!("{} in tenant {}", account.user.name, account.tenant_id))
        .chain(errors.iter().map(|e| (*e).to_owned()))
        .chain(notes.map(str::to_owned))
        .collect::<Vec<_>>()
        .join("; ");
    Some(check(name, errors.is_empty(), detail))
}

fn tmp_dir_ignored(config: &Config, found: &dyn Fn(&str) -> Option<String>) -> Option<DoctorCheck> {
    let code = Command::new(found("git")?)
        .arg("check-ignore")
        .arg("-q")
        // The trailing `/` lets a `tmp/` pattern match before the directory exists.
        .arg(format!("{}/", config.tmp_dir.display()))
        .current_dir(&config.project_root)
        .stderr(std::process::Stdio::null())
        .status()
        .ok()
        .and_then(|s| s.code());
    let name = "tmp_dir git-ignored";
    match code {
        Some(0) => Some(check(name, true, config.tmp_dir.display().to_string())),
        Some(1) => Some(check(
            name,
            false,
            format!(
                "add {} to .gitignore: saved plans and state snapshots hold secret values",
                config.tmp_dir.display()
            ),
        )),
        _ => None,
    }
}

/// `tfp self doctor`: is everything tf-project needs in place? Programs are looked up on `path` (tfp's own `PATH`
/// when none).
#[must_use]
pub fn do_self_doctor(config: &Config, path: Option<&std::ffi::OsStr>) -> Vec<DoctorCheck> {
    let binary = &config.terraform_binary;
    let path = path.map(ToOwned::to_owned).or_else(|| std::env::var_os("PATH"));
    let found = |program: &str| {
        which::which_in(program, path.clone(), &config.project_root)
            .ok()
            .map(|p| p.display().to_string())
    };
    let mut checks = vec![match found(binary) {
        Some(path) => check(format!("terraform binary `{binary}`"), true, path),
        None => check(
            format!("terraform binary `{binary}`"),
            false,
            "not found; install it or set `terraform_binary`",
        ),
    }];
    checks.extend(secret_checks(config, &found));
    for (name, dir, may_be_missing) in [
        ("terraform_dir", &config.terraform_dir, false),
        ("tfvars_dir", &config.tfvars_dir, false),
        ("tmp_dir", &config.tmp_dir, true),
        ("slots_dir", &config.slots_dir(), true),
    ] {
        let shown = dir.display();
        checks.push(match (dir.is_dir(), may_be_missing) {
            (true, _) => check(format!("{name} exists"), true, shown.to_string()),
            (false, true) => check(
                format!("{name} exists"),
                true,
                format!("{shown} (missing - will be created on demand)"),
            ),
            (false, false) => check(format!("{name} exists"), false, shown.to_string()),
        });
    }
    checks.extend(tmp_dir_ignored(config, &found));
    checks.extend(azure_identity_check(config, path.as_deref()));
    let scan = slots::scan_slots(config);
    checks.push(match slots::validate_unique(&scan.slots) {
        Ok(()) => {
            let notes = slots::find_shared_state(&scan.slots).into_iter().map(|s| {
                format!(
                    "; {:?} and {:?} share state {:?} (state_key)",
                    s.first.name,
                    s.second.name,
                    s.key()
                )
            });
            let detail: String = std::iter::once("all banners produce distinct slot names and states".to_owned())
                .chain(notes)
                .collect();
            check("slot uniqueness", true, detail)
        }
        Err(e) => check("slot uniqueness", false, e.to_string()),
    });
    checks.push(if scan.skipped.is_empty() {
        check("tfvars without a banner", true, "none")
    } else {
        let files: Vec<String> = scan.skipped.iter().map(ToString::to_string).collect();
        check("tfvars without a banner", false, files.join("; "))
    });
    let orphans: Vec<String> = slots::list_orphans(config).into_iter().map(|o| o.name).collect();
    checks.push(if orphans.is_empty() {
        check("orphan slots", true, "none")
    } else {
        check(
            "orphan slots",
            false,
            format!(
                "no banner defines {}; `tfp rm <slot>` removes a slot directory",
                orphans.join(", ")
            ),
        )
    });
    checks
}

/// `tfp self banner check`: what a tfvars file's banner resolves to.
///
/// # Errors
/// The file has no valid banner.
pub fn do_self_banner_check(config: &Config, ctx: &mut Ctx, tfvars: &Path) -> Result<Value> {
    let tfvars = crate::absolute(&ctx.cwd, tfvars);
    let banner = find_banner(&tfvars)?;
    if let Some(warning) = unknown_fields_warning(&banner, &tfvars) {
        say!(ctx.err, "{warning}");
    }
    render_summary(&banner, &tfvars, config)
}

/// The wrapped commands `tfp self trace` can show.
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum TraceCommand {
    /// `tfp use`.
    Use,
    /// `tfp init`.
    Init,
    /// `tfp plan`.
    Plan,
    /// `tfp apply`.
    Apply,
    /// `tfp refresh`.
    Refresh,
    /// `tfp destroy`.
    Destroy,
    /// `tfp output`.
    Output,
    /// `tfp import`.
    Import,
}

/// `tfp self trace`: the command line a wrapped command would run, with placeholders for what only a run knows.
///
/// # Errors
/// No active slot, or it cannot be resolved (`use`, `init`) or has no state (the rest).
pub fn do_self_trace(config: &Config, ctx: &Ctx, command: TraceCommand, flags: &Flags) -> Result<Vec<String>> {
    let name = require_active(config, ctx)?.name;
    if matches!(command, TraceCommand::Use | TraceCommand::Init) {
        let resolved = slots::resolve_slot(&name, config, &ctx.cwd)?;
        return Ok(build_init_argv(config, &resolved, &flags.extra));
    }
    let state = load_state(config, &name)?
        .ok_or_else(|| Error::Msg(format!("Slot {name:?} isn't initialised. Run `tfp use {name}` first.")))?;
    let var_file = "<DECRYPTED_TFVARS>";
    Ok(match command {
        TraceCommand::Plan => build_plan_argv(config, &state, var_file, flags),
        TraceCommand::Apply => build_apply_argv(config, &state, var_file, &flags.extra),
        TraceCommand::Refresh => build_targeted_argv(config, &state, "apply", var_file, flags),
        TraceCommand::Destroy => build_targeted_argv(config, &state, "destroy", var_file, flags),
        TraceCommand::Output => build_output_argv(config, &state, &flags.extra),
        _ => build_import_argv(
            config,
            &state,
            var_file,
            "<RESOURCE_ADDRESS>",
            "<RESOURCE_ID>",
            &flags.extra,
        ),
    })
}

/// `tfp last`: the active slot's last Terraform invocation.
///
/// # Errors
/// No active slot, or nothing recorded.
pub fn do_last_invocation(config: &Config, ctx: &Ctx) -> Result<LastInvocation> {
    let slot = require_active(config, ctx)?.name;
    let path = config.last_invocation(&slot);
    if !path.exists() {
        return Err(Error::Msg(format!(
            "No Terraform command has run in slot {slot:?} yet."
        )));
    }
    Ok(LastInvocation::load_file(&path)?)
}

/// A saved remote-state snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotEntry {
    /// The file.
    pub path: PathBuf,
    /// Its size in bytes.
    pub size: u64,
    /// When it was written.
    pub mtime: SystemTime,
}

/// `label` with anything but letters, digits, `-` and `_` replaced by `-`.
fn slugify(label: &str) -> String {
    let cleaned: String = label
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let trimmed = cleaned.trim_matches('-');
    if trimmed.is_empty() {
        "snapshot".to_owned()
    } else {
        trimmed.to_owned()
    }
}

/// The remote tfstate, from `terraform state pull`.
fn pull_remote_state(config: &Config, state: &MyState) -> Result<Vec<u8>> {
    let output = Command::new(&config.terraform_binary)
        .args([&format!("-chdir={}", state.source_root.display()), "state", "pull"])
        .envs(env_for_slot(config, state))
        .output()
        .map_err(|e| Error::Msg(format!("{}: {e}", config.terraform_binary)))?;
    if output.status.success() {
        return Ok(output.stdout);
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let detail = if stderr.trim().is_empty() {
        output.status.to_string()
    } else {
        stderr.trim().to_owned()
    };
    Err(Error::Msg(format!("terraform state pull failed: {detail}")))
}

/// The slot's snapshots, newest first.
fn list_snapshots(dir: &Path) -> Vec<SnapshotEntry> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut snapshots: Vec<SnapshotEntry> = entries
        .filter_map(std::result::Result::ok)
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|x| x == "tfstate"))
        .filter_map(|path| {
            let meta = std::fs::metadata(&path).ok().filter(std::fs::Metadata::is_file)?;
            Some(SnapshotEntry {
                size: meta.len(),
                mtime: meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                path,
            })
        })
        .collect();
    snapshots.sort_by(|a, b| b.mtime.cmp(&a.mtime).then_with(|| b.path.cmp(&a.path)));
    snapshots
}

/// Pull the remote state into the slot's snapshots, then keep only the newest `snapshot_retention`.
fn write_snapshot(config: &Config, state: &MyState, label: &str) -> Result<PathBuf> {
    let dir = config.snapshots_dir(&state.slot);
    std::fs::create_dir_all(&dir).map_err(io_error(&dir))?;
    let dest = dir.join(format!("{}-{}.tfstate", utc(SystemTime::now(), true), slugify(label)));
    std::fs::write(&dest, pull_remote_state(config, state)?).map_err(io_error(&dest))?;
    let keep = usize::try_from(config.snapshot_retention).unwrap_or(usize::MAX);
    for old in list_snapshots(&dir).into_iter().skip(keep) {
        // An old snapshot that cannot be deleted is not worth failing the command for.
        let _ = std::fs::remove_file(old.path);
    }
    Ok(dest)
}

/// Snapshot the remote state before a state-changing command; none when `snapshot_retention = 0`.
///
/// # Errors
/// The pull fails: the command should not go ahead without its safety net.
pub fn auto_snapshot(config: &Config, state: &MyState, op: &str) -> Result<Option<PathBuf>> {
    if config.snapshot_retention == 0 {
        return Ok(None);
    }
    write_snapshot(config, state, &format!("pre-{op}")).map(Some)
}

/// `tfp self snapshot create`: pull the remote state into the slot's snapshots, or to `dest` (outside retention).
///
/// # Errors
/// No usable active slot, or the pull or write fails.
pub fn do_self_snapshot(config: &Config, ctx: &Ctx, dest: Option<&Path>) -> Result<PathBuf> {
    let state = require_slot_state(config, &require_active(config, ctx)?.name)?;
    let Some(dest) = dest else {
        return write_snapshot(config, &state, "manual");
    };
    let dest = crate::absolute(&ctx.cwd, dest);
    let dir = dest.parent().unwrap_or(Path::new("/"));
    std::fs::create_dir_all(dir).map_err(io_error(dir))?;
    std::fs::write(&dest, pull_remote_state(config, &state)?).map_err(io_error(&dest))?;
    Ok(dest)
}

/// `tfp self snapshot list`: the active slot's snapshots, newest first.
///
/// # Errors
/// No usable active slot.
pub fn do_self_snapshot_list(config: &Config, ctx: &Ctx) -> Result<Vec<SnapshotEntry>> {
    let name = require_active(config, ctx)?.name;
    require_slot_state(config, &name)?;
    Ok(list_snapshots(&config.snapshots_dir(&name)))
}

/// `tfp self snapshot restore`: push a snapshot (by file name or stem; the newest by default) back with
/// `terraform state push -force`.
///
/// # Errors
/// No usable active slot, no such snapshot, or Terraform fails.
pub fn do_self_snapshot_restore(config: &Config, ctx: &mut Ctx, name: Option<&str>) -> Result<PathBuf> {
    let slot = require_active(config, ctx)?.name;
    let state = require_slot_state(config, &slot)?;
    let entries = list_snapshots(&config.snapshots_dir(&slot));
    let target = entries
        .iter()
        .find(|e| {
            name.is_none_or(|n| {
                e.path.file_name().is_some_and(|f| f == n) || e.path.file_stem().is_some_and(|f| f == n)
            })
        })
        .map(|e| e.path.clone());
    let Some(target) = target else {
        if entries.is_empty() {
            return Err(Error::Msg(format!(
                "Slot {slot:?} has no snapshots to restore. `tfp self snapshot create` takes one."
            )));
        }
        let names: Vec<String> = entries.iter().map(|e| e.path.display().to_string()).collect();
        return Err(Error::Msg(format!(
            "No snapshot named {:?} for slot {slot:?}. Available:\n  {}",
            name.unwrap_or_default(),
            names.join("\n  ")
        )));
    };
    let cmd = [
        config.terraform_binary.clone(),
        format!("-chdir={}", state.source_root.display()),
        "state".into(),
        "push".into(),
        "-force".into(),
        target.display().to_string(),
    ];
    // The live banner decides, as for the other commands; the saved state stands in when it is gone.
    let live = slots::resolve_slot(&slot, config, &ctx.cwd);
    let env = live.as_ref().map_or(&state.environ, |s| &s.env);
    let backend_config = live.as_ref().map_or(&state.backend_config, |s| &s.backend_config);
    identity::check_azure_identity(config, ctx, &slot, env, backend_config)?;
    confirm_protected(ctx, &slot, live.is_ok_and(|s| s.protected), "self snapshot restore")?;
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))?;
    Ok(target)
}

/// `tfp self providers lock`: `terraform providers lock` for `platforms`, or the defaults.
///
/// # Errors
/// No usable active slot, or Terraform fails.
pub fn do_self_providers_lock(config: &Config, ctx: &mut Ctx, platforms: &[String]) -> Result<()> {
    let state = require_slot_state(config, &require_active(config, ctx)?.name)?;
    let cmd = build_providers_lock_argv(config, &state.source_root, platforms);
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))
}

/// The slot's lock provider, its CLI taken from `[tf_project.programs]` and looked up on the context's `PATH`.
fn provider(config: &Config, state: &MyState, ctx: &Ctx) -> Result<LockProvider> {
    let mut provider = select_provider(state)?;
    provider.program = match provider.backend {
        Backend::Azure => config.programs.az.clone(),
        Backend::S3 => config.programs.aws.clone(),
    };
    provider.search_path.clone_from(&ctx.search_path);
    Ok(provider)
}

/// `tfp self lock status`.
///
/// # Errors
/// No usable active slot, an unsupported backend, or the cloud CLI fails.
pub fn do_self_lock_status(config: &Config, ctx: &Ctx) -> Result<LockStatus> {
    let state = require_slot_state(config, &require_active(config, ctx)?.name)?;
    provider(config, &state, ctx)?.status()
}

/// How `tfp self lock break` released the lock.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BreakResult {
    /// `terraform force-unlock` of this lock ID.
    ForceUnlock(String),
    /// The lease or item was broken at this backend.
    LeaseBreak(&'static str),
}

/// `tfp self lock break`: `terraform force-unlock` when the lock ID is known, falling back to breaking the lease at
/// the backend; straight to the backend with `blunt`.
///
/// # Errors
/// No usable active slot, an unsupported backend, no lock to break (politely), or the backend break fails.
pub fn do_self_lock_break(config: &Config, ctx: &mut Ctx, blunt: bool) -> Result<BreakResult> {
    let state = require_slot_state(config, &require_active(config, ctx)?.name)?;
    let provider = provider(config, &state, ctx)?;
    // If the status itself fails (say, a CLI auth blip), the user still asked for the lock to go: break it.
    if !blunt && let Ok(status) = provider.status() {
        let Some(id) = status.info.id else {
            return Err(Error::Msg(format!(
                "No lock to break on the {} backend. Pass --blunt only if you know a stale lease needs releasing.",
                provider.name()
            )));
        };
        let cmd = [
            config.terraform_binary.clone(),
            format!("-chdir={}", state.source_root.display()),
            "force-unlock".into(),
            "-force".into(),
            id.clone(),
        ];
        if terraform::run(ctx, &cmd, &env_for_slot(config, &state)).is_ok() {
            return Ok(BreakResult::ForceUnlock(id));
        }
    }
    provider.break_lease()?;
    Ok(BreakResult::LeaseBreak(provider.name()))
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::slugify;

    #[test]
    fn labels_become_safe_file_names() {
        assert_eq!(slugify("pre-state-rm"), "pre-state-rm");
        assert_eq!(slugify("a b/c_d"), "a-b-c_d");
        assert_eq!(slugify("//"), "snapshot");
    }
}
