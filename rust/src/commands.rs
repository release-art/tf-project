//! The wrapped Terraform commands. Each runs against the active slot's saved state.

use std::path::Path;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::config::Config;
use crate::models::{MyState, TfplanMeta, Versioned};
use crate::secrets::materialize;
use crate::slots::{self, ActiveSlot, ResolvedSlot};
use crate::state::{load_state, save_state, try_exclusive_lock, write_active};
use crate::terraform::{self, env_for_slot, merged_env, replace_args, target_args};
use crate::text::utc;
use crate::{Ctx, Error, Result, hex, io_error, say};

/// Linux is always locked, whatever the host; the host's platform and the configured extras are added.
const BASE_LOCK_PLATFORMS: [&str; 2] = ["linux_amd64", "linux_arm64"];

/// The `GOOS_GOARCH` Terraform calls this host.
#[must_use]
pub fn current_terraform_platform() -> String {
    terraform_platform(std::env::consts::OS, std::env::consts::ARCH)
}

/// Rust's names for an OS and architecture, in Go's spelling.
fn terraform_platform(os: &str, arch: &str) -> String {
    let os = if os == "macos" { "darwin" } else { os };
    let arch = match arch {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        arch => arch,
    };
    format!("{os}_{arch}")
}

/// Linux amd64 and arm64, the host, then `extras`, without repeats.
#[must_use]
pub fn default_lock_platforms(extras: &[String]) -> Vec<String> {
    let mut out: Vec<String> = BASE_LOCK_PLATFORMS.map(str::to_owned).to_vec();
    for platform in std::iter::once(current_terraform_platform()).chain(extras.iter().cloned()) {
        if !out.contains(&platform) {
            out.push(platform);
        }
    }
    out
}

/// The active slot.
///
/// # Errors
/// None is set by `-s`, `TFP_SLOT` or `tfp use`.
pub fn require_active(config: &Config, ctx: &Ctx) -> Result<ActiveSlot> {
    slots::resolve_active(config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref())?.ok_or_else(|| {
        Error::Msg("No active slot. Run `tfp use <slot>` first (or pass `-s <slot>` / set TFP_SLOT).".into())
    })
}

/// The slot's saved state.
///
/// # Errors
/// No banner defines the slot, or it was never `tfp use`d.
pub fn require_slot_state(config: &Config, slot: &str) -> Result<MyState> {
    if let Some(state) = load_state(config, slot)? {
        return Ok(state);
    }
    slots::assert_slot_exists(config, slot).map_err(|e| Error::Msg(e.to_string()))?;
    Err(Error::Msg(format!(
        "No state for slot {slot:?} at {}. Run `tfp use {slot}` first.",
        config.state_file(slot).display()
    )))
}

/// The active slot, as a wrapped command needs it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveState {
    /// The slot from its live banner; rebuilt from `state` when the banner is gone.
    resolved: ResolvedSlot,
    /// What `tfp use` saved.
    state: MyState,
    /// Whether `resolved` came from a live banner.
    banner_present: bool,
}

/// The active slot from its live banner, and its saved state. Without the banner, the slot is rebuilt from the saved
/// state.
fn resolve_active_slot(config: &Config, ctx: &Ctx) -> Result<ActiveState> {
    let active = require_active(config, ctx)?;
    let state = require_slot_state(config, &active.name)?;
    match slots::resolve_slot(&active.name, config, &ctx.cwd) {
        Ok(resolved) => Ok(ActiveState {
            resolved,
            state,
            banner_present: true,
        }),
        Err(Error::Slot(_)) => {
            let resolved = ResolvedSlot {
                name: state.slot.clone(),
                name_is_explicit: false,
                tfvars: state.tfvars.clone(),
                project: state.project.clone(),
                source_root: state.source_root.clone(),
                env: state.environ.clone(),
                backend_config: state.backend_config.clone(),
                slot_dir: config.slot_dir(&state.slot),
                protected: false,
                key_is_explicit: false,
            };
            Ok(ActiveState {
                resolved,
                state,
                banner_present: false,
            })
        }
        Err(e) => Err(e),
    }
}

/// Before `tfp <action>` changes a protected slot, have its name typed at the terminal; with no terminal,
/// `TFP_CONFIRM` must name it. `-y` and `--force` stand in for neither. `--dry-run` changes nothing, so asks nothing.
///
/// # Errors
/// The name typed, or `TFP_CONFIRM`, is not the slot's.
pub fn confirm_protected(ctx: &mut Ctx, slot: &str, protected: bool, action: &str) -> Result<()> {
    confirm_protected_with(ctx, slot, protected, action, &|| None)
}

/// [`confirm_protected`], showing what `preamble` says before asking; it runs only when someone will be asked.
fn confirm_protected_with(
    ctx: &mut Ctx,
    slot: &str,
    protected: bool,
    action: &str,
    preamble: &dyn Fn() -> Option<String>,
) -> Result<()> {
    if !protected || ctx.terraform.dry_run {
        return Ok(());
    }
    if !ctx.interactive {
        return match ctx.confirm_env.as_deref() {
            Some(name) if name == slot => Ok(()),
            Some(name) => Err(Error::Msg(format!(
                "slot {slot:?} is protected and TFP_CONFIRM={name:?} does not name it. Refusing `tfp {action}`."
            ))),
            None => Err(Error::Msg(format!(
                "slot {slot:?} is protected and there is no terminal to confirm at. \
                 Set TFP_CONFIRM={slot} to allow `tfp {action}`."
            ))),
        };
    }
    match preamble() {
        Some(text) => say!(ctx.err, "slot {slot:?} is protected. {text}"),
        None => {
            let _ = write!(ctx.err, "slot {slot:?} is protected. ");
        }
    }
    let _ = write!(ctx.err, "Type its name to run `tfp {action}`: ");
    let mut answer = String::new();
    let _ = ctx.input.read_line(&mut answer);
    if answer.trim() == slot {
        return Ok(());
    }
    Err(Error::Msg(format!("Aborted: that is not {slot:?}.")))
}

/// The parts of `terraform show -json <plan>` the protected-slot prompt reads. Terraform's format grows, so unknown
/// fields are ignored.
#[derive(Debug, Deserialize)]
struct ShownPlan {
    #[serde(default)]
    resource_changes: Vec<ResourceChange>,
}

#[derive(Debug, Deserialize)]
struct ResourceChange {
    address: String,
    change: Change,
}

#[derive(Debug, Deserialize)]
struct Change {
    actions: Vec<String>,
}

/// What a saved plan does, counted, with the resources it destroys or replaces.
#[derive(Debug, Default, PartialEq, Eq)]
struct PlanSummary {
    adds: usize,
    changes: usize,
    destroys: usize,
    replaces: usize,
    /// `  - <address>   (destroy|replace)`, one per resource.
    removals: Vec<String>,
}

impl PlanSummary {
    fn new(plan: ShownPlan) -> Self {
        let mut s = Self::default();
        for rc in plan.resource_changes {
            let actions: Vec<&str> = rc.change.actions.iter().map(String::as_str).collect();
            let kind = match actions[..] {
                ["create"] => {
                    s.adds += 1;
                    continue;
                }
                ["update"] => {
                    s.changes += 1;
                    continue;
                }
                ["delete"] => {
                    s.destroys += 1;
                    "destroy"
                }
                ["delete", "create"] | ["create", "delete"] => {
                    s.replaces += 1;
                    "replace"
                }
                _ => continue,
            };
            s.removals.push(format!("  - {}   ({kind})", rc.address));
        }
        s
    }

    fn describe(&self) -> String {
        let mut text = format!(
            "This plan adds {}, changes {}, destroys {}, replaces {}",
            self.adds, self.changes, self.destroys, self.replaces
        );
        if self.removals.is_empty() {
            text.push('.');
        } else {
            text = format!("{text}:\n{}", self.removals.join("\n"));
        }
        text
    }
}

/// What the slot's saved plan does, from `terraform show -json`; a warning instead when that fails, since the summary
/// must never be what stops an apply.
fn plan_summary(config: &Config, state: &MyState) -> String {
    let plan = std::process::Command::new(&config.terraform_binary)
        .args([
            chdir(&state.source_root),
            "show".into(),
            "-json".into(),
            config.tfplan_file(&state.slot).display().to_string(),
        ])
        .envs(env_for_slot(config, state))
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| serde_json::from_slice::<ShownPlan>(&o.stdout).ok());
    plan.map_or_else(
        || "warning: plan summary unavailable (`terraform show -json` failed).".to_owned(),
        |plan| PlanSummary::new(plan).describe(),
    )
}

/// Warn about tfvars files that define no slot, and refuse slots that clash ([`slots::validate_unique`]).
///
/// # Errors
/// Two slots share a name, or a remote state no banner asked to share.
pub fn check_slots(config: &Config, ctx: &mut Ctx) -> Result<()> {
    let scan = slots::scan_slots(config);
    for skipped in &scan.skipped {
        say!(ctx.err, "warning: no slot from {skipped}");
    }
    slots::validate_unique(&scan.slots)
}

/// [`crate::identity::check_azure_identity`] for a slot about to change.
fn check_identity(config: &Config, ctx: &mut Ctx, slot: &ResolvedSlot) -> Result<()> {
    crate::identity::check_azure_identity(config, ctx, &slot.name, &slot.env, &slot.backend_config)
}

/// Refuse to run Terraform in a slot whose `.terraform/` is gone.
fn check_data_dir(config: &Config, slot: &str) -> Result<()> {
    let data_dir = config.data_dir(slot);
    if data_dir.is_dir() {
        return Ok(());
    }
    Err(Error::Msg(format!(
        "slot {slot:?}: terraform data dir is missing at {}. It was probably deleted. \
         Run `tfp use {slot}` to reinitialize.",
        data_dir.display()
    )))
}

/// Refuse `apply` without both the saved plan and its sidecar.
fn check_apply_plan(config: &Config, slot: &str) -> Result<()> {
    let (plan, meta) = (config.tfplan_file(slot), config.tfplan_meta(slot));
    match (plan.exists(), meta.exists()) {
        (true, true) => Ok(()),
        (false, false) => Err(Error::Msg(format!(
            "slot {slot:?}: no saved tfplan at {}. Run `tfp plan` first.",
            plan.display()
        ))),
        (has_plan, _) => {
            let (present, missing) = if has_plan {
                ("tfplan", "tfplan.meta.json")
            } else {
                ("tfplan.meta.json", "tfplan")
            };
            Err(Error::Msg(format!(
                "slot {slot:?}: saved plan is half-deleted ({present} present, {missing} missing). \
                 Re-run `tfp plan` to regenerate both."
            )))
        }
    }
}

/// The files under `terraform_dir` whose edits stale a saved plan: sources (Terraform's and OpenTofu's), and the
/// variable files Terraform loads by itself. `.terraform.lock.hcl` is left out on purpose: `tfp use` on a sibling slot
/// of the same project rewrites it with `terraform providers lock`, which would stale every other slot's plan, and
/// Terraform already checks provider selections against a saved plan.
fn is_plan_input(name: &str) -> bool {
    const SUFFIXES: [&str; 6] = [
        ".tf",
        ".tf.json",
        ".tofu",
        ".tofu.json",
        ".auto.tfvars",
        ".auto.tfvars.json",
    ];
    matches!(name, "terraform.tfvars" | "terraform.tfvars.json") || SUFFIXES.iter().any(|s| name.ends_with(s))
}

/// SHA-256 over the decrypted tfvars and every [`is_plan_input`] file under `terraform_dir`, so editing this project
/// or a module it shares with others stales the plan. `.terraform/` directories are skipped.
// ponytail: local modules outside terraform_dir aren't seen; read .terraform/modules/modules.json if that layout shows up.
fn plan_inputs_sha256(terraform_dir: &Path, decrypted: &[u8]) -> Result<String> {
    let mut h = Sha256::new();
    h.update(b"tfvars:");
    h.update(decrypted);
    let tf_files = walkdir::WalkDir::new(terraform_dir)
        .sort_by_file_name()
        .into_iter()
        .filter_entry(|e| e.file_name() != ".terraform")
        .filter_map(std::result::Result::ok)
        .filter(|e| {
            let name = e.file_name().to_string_lossy();
            e.file_type().is_file() && is_plan_input(&name)
        });
    for entry in tf_files {
        let rel = entry.path().strip_prefix(terraform_dir).unwrap_or(entry.path());
        h.update(b"\x00tf:");
        h.update(rel.display().to_string());
        h.update(b"\x00");
        h.update(std::fs::read(entry.path()).map_err(io_error(entry.path()))?);
    }
    Ok(hex(&h.finalize()))
}

fn write_tfplan_meta(config: &Config, state: &MyState, resolved: &ResolvedSlot, decrypted: &[u8]) -> Result<()> {
    let meta = TfplanMeta {
        inputs_sha256: plan_inputs_sha256(&config.terraform_dir, decrypted)?,
        project: resolved.project.clone(),
        state_key: resolved.backend_config.get("key").cloned(),
        backend_config: resolved.backend_config.clone(),
        env: resolved.env.clone(),
    };
    Ok(meta.dump_file(&config.tfplan_meta(&state.slot))?)
}

/// Refuse a saved plan made from other inputs or another banner.
fn check_tfplan_fresh(config: &Config, ctx: &mut Ctx, active: &ActiveState, decrypted: &[u8]) -> Result<()> {
    let ActiveState {
        resolved,
        state,
        banner_present,
    } = active;
    let meta = TfplanMeta::load_file(&config.tfplan_meta(&state.slot))?;
    let actual = plan_inputs_sha256(&config.terraform_dir, decrypted)?;
    if meta.inputs_sha256 != actual {
        return Err(Error::Msg(format!(
            "Saved tfplan at {} was generated against different inputs (sha256 {}…) than the current tfvars + .tf \
             source (sha256 {}…). Re-run `tfp plan` or use `tfp apply --force`.",
            config.tfplan_file(&state.slot).display(),
            meta.inputs_sha256.get(..12).unwrap_or(&meta.inputs_sha256),
            actual.get(..12).unwrap_or(&actual),
        )));
    }
    // Without the banner, `resolved` comes from the same saved state the plan was made from: comparing would prove
    // nothing, so say so instead of pretending.
    if !*banner_present {
        say!(
            ctx.err,
            "warning: tfvars banner for slot {:?} is missing; skipping banner-drift check on apply. \
             Re-run `tfp use` once the banner is back to revalidate.",
            state.slot
        );
        return Ok(());
    }
    let key = resolved.backend_config.get("key");
    let mut drift = Vec::new();
    if meta.project != resolved.project {
        drift.push(format!("project {:?} → {:?}", meta.project, resolved.project));
    }
    if meta.state_key.as_ref() != key {
        drift.push(format!("state_key {:?} → {key:?}", meta.state_key));
    }
    if meta.backend_config != resolved.backend_config {
        drift.push("backend_config changed".to_owned());
    }
    if meta.env != resolved.env {
        drift.push("banner env changed".to_owned());
    }
    if drift.is_empty() {
        return Ok(());
    }
    Err(Error::Msg(format!(
        "Saved tfplan for slot {:?} no longer matches the current banner: {}. \
         Re-run `tfp plan` or use `tfp apply --force`.",
        state.slot,
        drift.join("; ")
    )))
}

/// `-chdir=<source_root>`.
fn chdir(source_root: &Path) -> String {
    format!("-chdir={}", source_root.display())
}

/// A command line: `parts` flattened.
fn argv<const N: usize>(parts: [&[String]; N]) -> Vec<String> {
    parts.concat()
}

/// `terraform init -upgrade -reconfigure` with the slot's `-backend-config`.
#[must_use]
pub fn build_init_argv(config: &Config, resolved: &ResolvedSlot, extra: &[String]) -> Vec<String> {
    let backend: Vec<String> = resolved
        .backend_config
        .iter()
        .flat_map(|(k, v)| ["-backend-config".to_owned(), format!("{k}={v}")])
        .collect();
    let head = [
        config.terraform_binary.clone(),
        chdir(&resolved.source_root),
        "init".into(),
        "-upgrade".into(),
        "-reconfigure".into(),
    ];
    argv([&head, extra, &backend])
}

/// `terraform providers lock` for each platform, once each; the defaults when `platforms` is empty.
#[must_use]
pub fn build_providers_lock_argv(config: &Config, source_root: &Path, platforms: &[String]) -> Vec<String> {
    let raw = if platforms.is_empty() {
        default_lock_platforms(&config.extra_lock_platforms)
    } else {
        platforms.to_vec()
    };
    let mut seen = Vec::new();
    for p in raw {
        if !seen.contains(&p) {
            seen.push(p);
        }
    }
    let flags: Vec<String> = seen.iter().map(|p| format!("-platform={p}")).collect();
    argv([
        &[
            config.terraform_binary.clone(),
            chdir(source_root),
            "providers".into(),
            "lock".into(),
        ],
        &flags,
    ])
}

/// What a wrapped command runs: the slot's `-chdir`, the subcommand, then `rest`.
fn wrapped(config: &Config, state: &MyState, subcommand: &str, rest: &[String]) -> Vec<String> {
    argv([
        &[
            config.terraform_binary.clone(),
            chdir(&state.source_root),
            subcommand.into(),
        ],
        rest,
    ])
}

/// `terraform plan`, saving the plan in the slot.
#[must_use]
pub fn build_plan_argv(config: &Config, state: &MyState, var_file: &str, flags: &Flags) -> Vec<String> {
    let out = format!("-out={}", config.tfplan_file(&state.slot).display());
    let rest = argv([
        &[format!("-var-file={var_file}")],
        &target_args(&flags.targets),
        &replace_args(&flags.replaces),
        &flags.extra,
        &[out],
    ]);
    wrapped(config, state, "plan", &rest)
}

/// `terraform apply` of the saved plan.
#[must_use]
pub fn build_apply_argv(config: &Config, state: &MyState, var_file: &str, extra: &[String]) -> Vec<String> {
    let plan = config.tfplan_file(&state.slot).display().to_string();
    wrapped(
        config,
        state,
        "apply",
        &argv([&[format!("-var-file={var_file}")], extra, &[plan]]),
    )
}

/// `terraform apply` without a saved plan (`refresh`), or `terraform destroy`.
#[must_use]
pub fn build_targeted_argv(
    config: &Config,
    state: &MyState,
    subcommand: &str,
    var_file: &str,
    flags: &Flags,
) -> Vec<String> {
    let rest = argv([
        &[format!("-var-file={var_file}")],
        &target_args(&flags.targets),
        &flags.extra,
    ]);
    wrapped(config, state, subcommand, &rest)
}

/// `terraform import <address> <id>`.
#[must_use]
pub fn build_import_argv(
    config: &Config,
    state: &MyState,
    var_file: &str,
    address: &str,
    id: &str,
    extra: &[String],
) -> Vec<String> {
    let rest = argv([
        &[format!("-var-file={var_file}")],
        extra,
        &[address.to_owned(), id.to_owned()],
    ]);
    wrapped(config, state, "import", &rest)
}

/// `terraform output -json`.
#[must_use]
pub fn build_output_argv(config: &Config, state: &MyState, extra: &[String]) -> Vec<String> {
    wrapped(config, state, "output", &argv([&["-json".to_owned()], extra]))
}

/// Targets, replacements and anything else for Terraform, as given on the command line.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Flags {
    /// `-t` / `--target`.
    pub targets: Vec<String>,
    /// `-r` / `--replace`.
    pub replaces: Vec<String>,
    /// Passed through verbatim.
    pub extra: Vec<String>,
}

/// What the slot's backend was initialised against: Terraform, the sources, and `-backend-config`.
fn backend_signature(resolved: &ResolvedSlot, terraform_binary: &str) -> String {
    let mut h = Sha256::new();
    h.update(b"tf:");
    h.update(terraform_binary);
    h.update(b"\x00src:");
    h.update(resolved.source_root.display().to_string());
    for (key, value) in &resolved.backend_config {
        h.update(b"\x00bc:");
        h.update(key);
        h.update(b"=");
        h.update(value);
    }
    hex(&h.finalize())
}

/// Why the slot needs `terraform init`; none when it is warm.
fn init_reason(config: &Config, resolved: &ResolvedSlot) -> Option<&'static str> {
    if !resolved.data_dir().is_dir() {
        return Some("first init (no .terraform/)");
    }
    match std::fs::read_to_string(config.backend_hash(&resolved.name)) {
        Err(_) => Some("missing backend.hash"),
        Ok(saved) if saved.trim() != backend_signature(resolved, &config.terraform_binary) => {
            Some("backend_config changed")
        }
        Ok(_) => None,
    }
}

/// `tfp use`: make the slot `arg` names (a slot name or a tfvars path) active, running `terraform init` only when
/// the slot is cold or its backend changed.
///
/// # Errors
/// The slot cannot be resolved, two banners claim it, or `terraform init` fails.
pub fn do_use(config: &Config, ctx: &mut Ctx, arg: &str, extra: &[String]) -> Result<ResolvedSlot> {
    check_slots(config, ctx)?;
    let resolved = slots::resolve_slot(arg, config, &ctx.cwd)?;
    if arg != resolved.name && slots::looks_like_path(arg) {
        say!(ctx.err, "resolved {arg} → slot {:?}", resolved.name);
    }
    bind_slot(config, ctx, &resolved, false, extra)?;
    write_active(config, &resolved.name)?;
    say!(
        ctx.err,
        "active slot → {} (file: {}; affects all shells without TFP_SLOT set)",
        resolved.name,
        config.active_file().display()
    );
    Ok(resolved)
}

/// `tfp init`: re-read the active slot's banner and re-initialise its backend, without changing the active slot.
/// Always runs `terraform init`.
///
/// # Errors
/// As [`do_use`].
pub fn do_init(config: &Config, ctx: &mut Ctx, extra: &[String]) -> Result<ResolvedSlot> {
    check_slots(config, ctx)?;
    let active = require_active(config, ctx)?;
    let resolved = slots::resolve_slot(&active.name, config, &ctx.cwd)?;
    bind_slot(config, ctx, &resolved, true, extra)?;
    Ok(resolved)
}

/// Saved plans and state snapshots hold secret values in plain text: only the owner may read `dir`. Windows
/// directories already inherit the user's ACLs.
#[cfg(unix)]
fn make_private(dir: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn make_private(_: &Path) -> std::io::Result<()> {
    Ok(())
}

/// Initialise the slot when needed (always with `force_init`) and save its state.
fn bind_slot(
    config: &Config,
    ctx: &mut Ctx,
    resolved: &ResolvedSlot,
    force_init: bool,
    extra: &[String],
) -> Result<()> {
    let reason = init_reason(config, resolved).or(force_init.then_some("explicit re-init"));
    let first_init = !resolved.slot_dir.exists();
    std::fs::create_dir_all(&resolved.slot_dir)
        .and_then(|()| make_private(&config.tmp_dir))
        .map_err(io_error(&resolved.slot_dir))?;
    if let Some(cache) = &config.plugin_cache_dir {
        std::fs::create_dir_all(cache).map_err(io_error(cache))?;
    }
    if let Some(reason) = reason {
        say!(
            ctx.out,
            "==== PROJECT: {} (slot {:?}, {reason}) ====",
            resolved.project,
            resolved.name
        );
        let env = merged_env(
            &resolved.env,
            Some(&resolved.data_dir()),
            config.plugin_cache_dir.as_deref(),
        );
        let init = terraform::run(ctx, &build_init_argv(config, resolved, extra), &env).and_then(|()| {
            terraform::run(
                ctx,
                &build_providers_lock_argv(config, &resolved.source_root, &[]),
                &env,
            )
        });
        if let Err(e) = init {
            // A failed first init leaves nothing worth keeping; a failed re-init keeps the last good state.
            if first_init {
                let _ = std::fs::remove_dir_all(&resolved.slot_dir);
            } else {
                say!(
                    ctx.err,
                    "init failed; slot {:?} retains its previous saved state - fix the cause and re-run `tfp use`.",
                    resolved.name
                );
            }
            return Err(e);
        }
        let hash = config.backend_hash(&resolved.name);
        std::fs::write(&hash, backend_signature(resolved, &config.terraform_binary) + "\n").map_err(io_error(&hash))?;
    }
    let state = MyState {
        slot: resolved.name.clone(),
        project: resolved.project.clone(),
        tfvars: resolved.tfvars.clone(),
        source_root: resolved.source_root.clone(),
        tfplan_location: config.tfplan_file(&resolved.name),
        environ: resolved.env.clone(),
        backend_config: resolved.backend_config.clone(),
    };
    save_state(config, &state)
}

/// `tfp rm`: delete a slot's directory (not its tfvars file). The active slot needs `force`.
///
/// # Errors
/// The slot is active, or has no directory.
pub fn do_rm(config: &Config, ctx: &Ctx, name: &str, force: bool) -> Result<std::path::PathBuf> {
    if let Some(active) = slots::resolve_active(config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref())?
        && active.name == name
        && !force
    {
        return Err(Error::Msg(format!(
            "slot {name:?} is the active slot ({}). Pass --force to remove it anyway.",
            active.source
        )));
    }
    let slot_dir = config.slot_dir(name);
    if !slot_dir.exists() {
        return Err(Error::Msg(format!("no slot directory at {}", slot_dir.display())));
    }
    std::fs::remove_dir_all(&slot_dir).map_err(io_error(&slot_dir))?;
    Ok(slot_dir)
}

/// `tfp plan`: plan against the decrypted tfvars and save the plan with its sidecar.
///
/// # Errors
/// No usable active slot, or Terraform fails.
pub fn do_plan(config: &Config, ctx: &mut Ctx, flags: &Flags) -> Result<()> {
    let ActiveState { resolved, state, .. } = resolve_active_slot(config, ctx)?;
    check_data_dir(config, &state.slot)?;
    let decrypted = materialize(config, ctx, &state.tfvars)?;
    let var_file = decrypted.path().display().to_string();
    terraform::run(
        ctx,
        &build_plan_argv(config, &state, &var_file, flags),
        &env_for_slot(config, &state),
    )?;
    write_tfplan_meta(config, &state, &resolved, decrypted.contents())
}

/// `tfp apply`: apply the saved plan, refusing one made from other inputs unless `force`; the plan is spent after.
///
/// # Errors
/// No saved plan, a stale one, another apply running, or Terraform fails.
pub fn do_apply(config: &Config, ctx: &mut Ctx, force: bool, extra: &[String]) -> Result<()> {
    let active = resolve_active_slot(config, ctx)?;
    let state = &active.state;
    check_data_dir(config, &state.slot)?;
    check_apply_plan(config, &state.slot)?;
    let _lock = try_exclusive_lock(&config.apply_lock(&state.slot))?;
    let decrypted = materialize(config, ctx, &state.tfvars)?;
    if !force {
        check_tfplan_fresh(config, ctx, &active, decrypted.contents())?;
    }
    check_identity(config, ctx, &active.resolved)?;
    confirm_protected_with(ctx, &state.slot, active.resolved.protected, "apply", &|| {
        Some(plan_summary(config, state))
    })?;
    let var_file = decrypted.path().display().to_string();
    terraform::run(
        ctx,
        &build_apply_argv(config, state, &var_file, extra),
        &env_for_slot(config, state),
    )?;
    let _ = std::fs::remove_file(config.tfplan_file(&state.slot));
    let _ = std::fs::remove_file(config.tfplan_meta(&state.slot));
    Ok(())
}

/// `tfp refresh` (`apply` with no saved plan) or `tfp destroy`, under the apply lock.
///
/// # Errors
/// No usable active slot, another apply running, or Terraform fails.
pub fn do_targeted(config: &Config, ctx: &mut Ctx, subcommand: &str, flags: &Flags) -> Result<()> {
    let ActiveState { resolved, state, .. } = resolve_active_slot(config, ctx)?;
    check_data_dir(config, &state.slot)?;
    let action = if subcommand == "apply" { "refresh" } else { subcommand };
    check_identity(config, ctx, &resolved)?;
    confirm_protected(ctx, &state.slot, resolved.protected, action)?;
    let _lock = try_exclusive_lock(&config.apply_lock(&state.slot))?;
    let decrypted = materialize(config, ctx, &state.tfvars)?;
    let var_file = decrypted.path().display().to_string();
    let cmd = build_targeted_argv(config, &state, subcommand, &var_file, flags);
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))
}

/// `tfp fmt`: `terraform fmt -recursive` over the terraform and tfvars directories.
///
/// # Errors
/// Neither directory exists, or Terraform fails.
pub fn do_fmt(config: &Config, ctx: &mut Ctx, extra: &[String]) -> Result<()> {
    let paths: Vec<String> = [&config.terraform_dir, &config.tfvars_dir]
        .into_iter()
        .filter(|p| p.exists())
        .map(|p| p.display().to_string())
        .collect();
    if paths.is_empty() {
        return Err(Error::Msg(format!(
            "Neither {} nor {} exists to format.",
            config.terraform_dir.display(),
            config.tfvars_dir.display()
        )));
    }
    let cmd = argv([
        &[config.terraform_binary.clone(), "fmt".into(), "-recursive".into()],
        extra,
        &paths,
    ]);
    terraform::run(ctx, &cmd, &terraform::Env::new())
}

/// `tfp output`.
///
/// # Errors
/// No usable active slot, or Terraform fails.
pub fn do_output(config: &Config, ctx: &mut Ctx, extra: &[String]) -> Result<()> {
    let ActiveState { state, .. } = resolve_active_slot(config, ctx)?;
    check_data_dir(config, &state.slot)?;
    terraform::run(
        ctx,
        &build_output_argv(config, &state, extra),
        &env_for_slot(config, &state),
    )
}

/// `tfp import`, after snapshotting the remote state.
///
/// # Errors
/// No usable active slot, the snapshot fails, or Terraform fails.
pub fn do_import(config: &Config, ctx: &mut Ctx, address: &str, id: &str, extra: &[String]) -> Result<()> {
    let ActiveState { resolved, state, .. } = resolve_active_slot(config, ctx)?;
    check_data_dir(config, &state.slot)?;
    check_identity(config, ctx, &resolved)?;
    confirm_protected(ctx, &state.slot, resolved.protected, "import")?;
    crate::self_commands::auto_snapshot(config, &state, "import")?;
    let decrypted = materialize(config, ctx, &state.tfvars)?;
    let var_file = decrypted.path().display().to_string();
    let cmd = build_import_argv(config, &state, &var_file, address, id, extra);
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))
}

/// `tfp state <subcommand>`; `rm`, `mv` and `replace-provider` snapshot the remote state first.
///
/// # Errors
/// No usable active slot, the snapshot fails, or Terraform fails.
pub fn do_state(config: &Config, ctx: &mut Ctx, subcommand: &str, args: &[String], extra: &[String]) -> Result<()> {
    let ActiveState { resolved, state, .. } = resolve_active_slot(config, ctx)?;
    check_data_dir(config, &state.slot)?;
    let mutates = matches!(subcommand, "rm" | "mv" | "push" | "replace-provider");
    if mutates {
        check_identity(config, ctx, &resolved)?;
    }
    confirm_protected(
        ctx,
        &state.slot,
        mutates && resolved.protected,
        &format!("state {subcommand}"),
    )?;
    if matches!(subcommand, "rm" | "mv" | "replace-provider") {
        crate::self_commands::auto_snapshot(config, &state, &format!("state-{subcommand}"))?;
    }
    let cmd = wrapped(config, &state, "state", &argv([&[subcommand.to_owned()], extra, args]));
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))
}

/// `tfp tf ...`: any Terraform command, verbatim, in the active slot (`-chdir` and its environment) when there is one.
///
/// # Errors
/// The active slot has no saved state or data dir, or Terraform fails.
pub fn do_tf(config: &Config, ctx: &mut Ctx, args: &[String]) -> Result<()> {
    let Some(active) = slots::resolve_active(config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref())? else {
        let cmd = argv([std::slice::from_ref(&config.terraform_binary), args]);
        return terraform::run(ctx, &cmd, &terraform::Env::new());
    };
    let state = require_slot_state(config, &active.name)?;
    check_data_dir(config, &state.slot)?;
    let cmd = argv([&[config.terraform_binary.clone(), chdir(&state.source_root)], args]);
    terraform::run(ctx, &cmd, &env_for_slot(config, &state))
}

/// How one slot came out of `tfp drift`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DriftResult {
    /// Nothing to change.
    Clean,
    /// The real infrastructure differs from the code.
    Drift,
    /// The plan could not run.
    Error,
    /// Not checked.
    Skipped,
}

/// One slot's line in `tfp drift`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DriftEntry {
    /// The slot.
    pub slot: String,
    /// What the plan found.
    pub result: DriftResult,
    /// Why it was skipped, or what failed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// `tfp drift`: `terraform plan -detailed-exitcode` in every initialised, unprotected slot, one after the other.
/// No plan is saved, so a slot's saved plan is left as it is, and the active slot does not move.
pub fn do_drift(config: &Config, ctx: &mut Ctx) -> Vec<DriftEntry> {
    let last = ctx.terraform.last_invocation.take();
    let entries = slots::list_slots(config)
        .iter()
        .map(|slot| drift_one(config, ctx, slot))
        .collect();
    ctx.terraform.last_invocation = last;
    entries
}

fn drift_one(config: &Config, ctx: &mut Ctx, slot: &ResolvedSlot) -> DriftEntry {
    let entry = |result, reason: Option<String>| DriftEntry {
        slot: slot.name.clone(),
        result,
        reason,
    };
    let state = match load_state(config, &slot.name) {
        _ if slot.protected => Err(entry(DriftResult::Skipped, Some("protected".into()))),
        Ok(Some(state)) => Ok(state),
        Ok(None) => Err(entry(
            DriftResult::Skipped,
            Some(format!("not initialised - run `tfp use {}`", slot.name)),
        )),
        Err(e) => Err(entry(DriftResult::Error, Some(e.to_string()))),
    };
    let state = match state {
        Ok(state) => state,
        Err(done) => {
            say!(ctx.err, "slot {:?}: {}", slot.name, describe_drift(&done));
            return done;
        }
    };
    say!(ctx.err, "==== drift: slot {:?} ====", slot.name);
    ctx.terraform.last_invocation = Some(config.last_invocation(&slot.name));
    match drift_plan(config, ctx, &state) {
        Ok(()) => entry(DriftResult::Clean, None),
        Err(Error::Exit(2)) => entry(DriftResult::Drift, None),
        Err(e) => entry(DriftResult::Error, Some(e.to_string())),
    }
}

/// `clean`, `drift`, or the result with its reason.
#[must_use]
pub fn describe_drift(entry: &DriftEntry) -> String {
    let result = serde_json::to_value(entry.result).unwrap_or_default();
    let result = result.as_str().unwrap_or_default();
    entry
        .reason
        .as_ref()
        .map_or_else(|| result.to_owned(), |reason| format!("{result}: {reason}"))
}

fn drift_plan(config: &Config, ctx: &mut Ctx, state: &MyState) -> Result<()> {
    check_data_dir(config, &state.slot)?;
    let decrypted = materialize(config, ctx, &state.tfvars)?;
    let rest = [
        format!("-var-file={}", decrypted.path().display()),
        "-detailed-exitcode".into(),
        "-lock=false".into(),
        "-input=false".into(),
    ];
    terraform::run(
        ctx,
        &wrapped(config, state, "plan", &rest),
        &env_for_slot(config, state),
    )
}

/// What `tfp status` shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatusReport {
    /// The active slot, and where its name came from.
    pub active: ActiveSlot,
    /// Its saved state.
    pub state: MyState,
    /// Whether a saved plan is waiting.
    pub plan_ready: bool,
    /// Whether the slot's banner protects it.
    pub protected: bool,
}

/// The active slot's state, if it has been `tfp use`d.
///
/// # Errors
/// The state file is unreadable.
pub fn status_report(config: &Config, ctx: &Ctx) -> Result<Option<StatusReport>> {
    let Some(active) = slots::resolve_active(config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref())? else {
        return Ok(None);
    };
    let Some(state) = load_state(config, &active.name)? else {
        return Ok(None);
    };
    let plan_ready = config.tfplan_file(&state.slot).exists();
    let protected = slots::resolve_slot(&active.name, config, &ctx.cwd).is_ok_and(|s| s.protected);
    Ok(Some(StatusReport {
        active,
        state,
        plan_ready,
        protected,
    }))
}

/// One row of `tfp ls`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SlotListing {
    /// The slot.
    pub name: String,
    /// Its project.
    pub project: String,
    /// Its tfvars file.
    pub tfvars: std::path::PathBuf,
    /// Its backend `key`.
    pub backend_key: Option<String>,
    /// Whether `terraform init` has populated its providers.
    pub warm: bool,
    /// When, if so.
    pub warm_mtime: Option<String>,
    /// Whether it is the active slot.
    pub is_active: bool,
    /// Whether its banner protects it.
    pub protected: bool,
}

/// Every slot, with whether it is warm and active.
///
/// # Errors
/// `tmp/active` is unreadable.
pub fn slot_listing(config: &Config, ctx: &Ctx) -> Result<Vec<SlotListing>> {
    let active = slots::resolve_active(config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref())?.map(|a| a.name);
    Ok(slots::list_slots(config)
        .into_iter()
        .map(|s| {
            // A slot is warm once `terraform init` has filled `.terraform/providers/`, whatever the backend.
            let warm_mtime = std::fs::metadata(s.data_dir().join("providers"))
                .and_then(|m| m.modified())
                .ok()
                .map(|t| utc(t, false));
            SlotListing {
                is_active: active.as_ref() == Some(&s.name),
                protected: s.protected,
                backend_key: s.backend_config.get("key").cloned(),
                warm: warm_mtime.is_some(),
                warm_mtime,
                name: s.name,
                project: s.project,
                tfvars: s.tfvars,
            }
        })
        .collect())
}

#[cfg(test)]
pub(crate) mod tests {
    use pretty_assertions::assert_eq;

    use super::*;
    use crate::testing::{Project, Streams};

    pub(crate) const DEMO: &str = r#"{"header":"terraform","project":"demo","state_key":"/demo/dev.tfstate"}"#;

    /// A project with slot `dev` bound and active, and a fake terraform that runs `body`.
    pub(crate) fn bound(body: &str) -> (Project, Config) {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        p.write("terraform/demo/main.tf", "resource {}\n");
        p.fake_terraform(&format!(
            "[ -n \"$TF_DATA_DIR\" ] && mkdir -p \"$TF_DATA_DIR/providers\"\n{body}"
        ));
        let c = p.config();
        let mut s = Streams::default();
        do_use(&c, &mut s.ctx(&p.root), "dev", &[]).unwrap();
        std::fs::remove_file(p.path("terraform.log")).unwrap();
        (p, c)
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn platforms() {
        assert_eq!(terraform_platform("macos", "aarch64"), "darwin_arm64");
        assert_eq!(terraform_platform("linux", "x86_64"), "linux_amd64");
        assert_eq!(terraform_platform("freebsd", "riscv64"), "freebsd_riscv64");
        let host = current_terraform_platform();
        assert!(host.contains('_'));
        let all = default_lock_platforms(&strings(&["windows_amd64", "linux_amd64"]));
        assert_eq!(all[..2], strings(&["linux_amd64", "linux_arm64"]));
        assert!(all.contains(&host) && all.ends_with(&strings(&["windows_amd64"])));
    }

    #[test]
    fn use_makes_tmp_dir_private() {
        use std::os::unix::fs::PermissionsExt;
        let (_p, c) = bound("");
        let mode = std::fs::metadata(&c.tmp_dir).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o700);
    }

    #[test]
    fn use_inits_once_then_switches_warm() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        p.fake_terraform("mkdir -p \"$TF_DATA_DIR/providers\"");
        let c = p.config();
        let mut s = Streams::default();
        let resolved = do_use(&c, &mut s.ctx(&p.root), "tfvars/dev.tfvars", &strings(&["-no-color"])).unwrap();
        assert_eq!(resolved.name, "dev");
        let calls = p.terraform_calls();
        let chdir = format!("-chdir={}", p.path("terraform/demo").display());
        assert_eq!(
            calls[0],
            format!("{chdir} init -upgrade -reconfigure -no-color -backend-config key=demo/dev.tfstate")
        );
        assert!(calls[1].starts_with(&format!(
            "{chdir} providers lock -platform=linux_amd64 -platform=linux_arm64"
        )));
        assert_eq!(
            s.out(),
            "==== PROJECT: demo (slot \"dev\", first init (no .terraform/)) ====\n"
        );
        assert!(
            s.err()
                .starts_with("resolved tfvars/dev.tfvars → slot \"dev\"\nactive slot → dev (file: ")
        );
        assert_eq!(crate::state::read_active(&c).unwrap().as_deref(), Some("dev"));
        assert_eq!(
            load_state(&c, "dev").unwrap().unwrap().backend_config["key"],
            "demo/dev.tfstate"
        );
        // Warm: no init.
        do_use(&c, &mut Streams::default().ctx(&p.root), "dev", &[]).unwrap();
        assert_eq!(p.terraform_calls().len(), 2);
        // A changed backend re-inits; so does a lost hash.
        p.tfvars(
            "dev",
            r#"{"header":"terraform","project":"demo","state_key":"/other.tfstate"}"#,
        );
        let mut s = Streams::default();
        do_use(&c, &mut s.ctx(&p.root), "dev", &[]).unwrap();
        assert!(s.out().contains("backend_config changed"));
        std::fs::remove_file(c.backend_hash("dev")).unwrap();
        let mut s = Streams::default();
        do_use(&c, &mut s.ctx(&p.root), "dev", &[]).unwrap();
        assert!(s.out().contains("missing backend.hash"));
    }

    #[test]
    fn init_reinits_without_moving_the_active_slot() {
        let (p, c) = bound("");
        p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.slot_flag = Some("qa".into());
        assert_eq!(do_init(&c, &mut ctx, &[]).unwrap().name, "qa");
        assert!(s.out().contains("first init"));
        assert_eq!(crate::state::read_active(&c).unwrap().as_deref(), Some("dev"));
        let mut s = Streams::default();
        do_init(&c, &mut s.ctx(&p.root), &[]).unwrap();
        assert!(s.out().contains("explicit re-init"));
    }

    #[test]
    fn failed_init_cleans_up_only_a_first_init() {
        let (p, c) = bound("[ \"$2\" = init ] && [ -f \"$(dirname \"$0\")/fail\" ] && exit 1\nexit 0");
        p.write("bin/fail", "");
        let mut s = Streams::default();
        assert_eq!(do_init(&c, &mut s.ctx(&p.root), &[]), Err(Error::Exit(1)));
        assert!(s.err().contains("retains its previous saved state"));
        assert!(c.state_file("dev").exists());
        p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        assert_eq!(
            do_use(&c, &mut Streams::default().ctx(&p.root), "qa", &[]),
            Err(Error::Exit(1))
        );
        assert!(!c.slot_dir("qa").exists());
    }

    #[test]
    fn use_refuses_conflicts_and_unknown_slots() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        p.tfvars("dup", r#"{"header":"terraform","project":"demo","slot":"dev"}"#);
        let c = p.config();
        let mut s = Streams::default();
        assert!(matches!(
            do_use(&c, &mut s.ctx(&p.root), "dev", &[]),
            Err(Error::Slot(_))
        ));
        assert!(matches!(do_init(&c, &mut s.ctx(&p.root), &[]), Err(Error::Slot(_))));
        std::fs::remove_file(p.path("tfvars/dup.tfvars")).unwrap();
        assert!(matches!(do_init(&c, &mut s.ctx(&p.root), &[]), Err(Error::Msg(m)) if m.starts_with("No active slot")));
    }

    #[test]
    fn plan_then_apply() {
        let (p, c) = bound("");
        let mut s = Streams::default();
        let flags = Flags {
            targets: strings(&["a.b"]),
            replaces: strings(&["c.d"]),
            extra: strings(&["-lock=false"]),
        };
        do_plan(&c, &mut s.ctx(&p.root), &flags).unwrap();
        let tfvars = p.path("tfvars/dev.tfvars").display().to_string();
        let plan = c.tfplan_file("dev").display().to_string();
        let chdir = format!("-chdir={}", p.path("terraform/demo").display());
        assert_eq!(
            p.terraform_calls(),
            [format!(
                "{chdir} plan -var-file={tfvars} -target=a.b -replace=c.d -lock=false -out={plan}"
            )]
        );
        assert!(c.tfplan_meta("dev").exists());
        // The fake terraform writes no plan; stand in for it.
        std::fs::write(&plan, "").unwrap();
        do_apply(&c, &mut s.ctx(&p.root), false, &[]).unwrap();
        assert_eq!(
            p.terraform_calls()[1],
            format!("{chdir} apply -var-file={tfvars} {plan}")
        );
        assert!(!c.tfplan_meta("dev").exists() && !Path::new(&plan).exists());
    }

    fn planned() -> (Project, Config) {
        let (p, c) = bound("");
        do_plan(&c, &mut Streams::default().ctx(&p.root), &Flags::default()).unwrap();
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        (p, c)
    }

    fn apply_error(p: &Project, c: &Config, force: bool) -> String {
        do_apply(c, &mut Streams::default().ctx(&p.root), force, &[])
            .unwrap_err()
            .to_string()
    }

    #[test]
    fn apply_refuses_stale_plans() {
        let (p, c) = planned();
        p.write("terraform/demo/main.tf", "changed\n");
        assert!(apply_error(&p, &c, false).starts_with("Saved tfplan at "));
        do_apply(&c, &mut Streams::default().ctx(&p.root), true, &[]).unwrap();

        // A shared module stales the plan; a stray `.terraform/` and other files don't.
        let (p, c) = planned();
        p.write("terraform/demo/.terraform/modules/x/main.tf", "cached\n");
        p.write("terraform/demo/notes.md", "notes\n");
        p.write("terraform/demo/.terraform.lock.hcl", "relocked\n");
        p.write("terraform/demo/other.tfvars", "x = 2\n");
        do_apply(&c, &mut Streams::default().ctx(&p.root), false, &[]).unwrap();
        for module in [
            "terraform/modules/db/main.tf",
            "terraform/modules/db/vars.tf.json",
            "terraform/demo/main.tofu",
            "terraform/demo/vars.tofu.json",
            "terraform/demo/terraform.tfvars",
            "terraform/demo/terraform.tfvars.json",
            "terraform/demo/x.auto.tfvars",
            "terraform/demo/x.auto.tfvars.json",
        ] {
            let (p, c) = planned();
            p.write(module, "{}\n");
            assert!(apply_error(&p, &c, false).starts_with("Saved tfplan at "));
        }

        let (p, c) = planned();
        p.tfvars(
            "dev",
            r#"{"header":"terraform","project":"demo","state_key":"/x.tfstate","env":{"A":"1"}}"#,
        );
        resync_meta(&p, &c);
        let mut meta = TfplanMeta::load_file(&c.tfplan_meta("dev")).unwrap();
        meta.project = "old".into();
        meta.dump_file(&c.tfplan_meta("dev")).unwrap();
        assert_eq!(
            apply_error(&p, &c, false),
            "Saved tfplan for slot \"dev\" no longer matches the current banner: project \"old\" → \"demo\"; \
             state_key Some(\"demo/dev.tfstate\") → Some(\"x.tfstate\"); backend_config changed; banner env changed. \
             Re-run `tfp plan` or use `tfp apply --force`."
        );
    }

    /// Make the saved plan's input hash match the tfvars as they are now.
    fn resync_meta(p: &Project, c: &Config) {
        let mut meta = TfplanMeta::load_file(&c.tfplan_meta("dev")).unwrap();
        meta.inputs_sha256 =
            plan_inputs_sha256(&c.terraform_dir, &std::fs::read(p.path("tfvars/dev.tfvars")).unwrap()).unwrap();
        meta.dump_file(&c.tfplan_meta("dev")).unwrap();
    }

    #[test]
    fn apply_without_a_banner_warns_and_skips_the_drift_check() {
        let (p, c) = planned();
        p.write("tfvars/dev.tfvars", "x = 1\n");
        resync_meta(&p, &c);
        let mut s = Streams::default();
        do_apply(&c, &mut s.ctx(&p.root), false, &[]).unwrap();
        assert!(
            s.err()
                .starts_with("warning: tfvars banner for slot \"dev\" is missing")
        );
    }

    #[test]
    fn apply_preflight() {
        let (p, c) = bound("");
        assert!(apply_error(&p, &c, false).ends_with("Run `tfp plan` first."));
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        assert!(apply_error(&p, &c, false).contains("(tfplan present, tfplan.meta.json missing)"));
        std::fs::remove_file(c.tfplan_file("dev")).unwrap();
        std::fs::write(c.tfplan_meta("dev"), "").unwrap();
        assert!(apply_error(&p, &c, false).contains("(tfplan.meta.json present, tfplan missing)"));
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        let _held = crate::state::exclusive_lock(&c.apply_lock("dev")).unwrap();
        assert!(apply_error(&p, &c, false).starts_with("Lock held by another process"));
        std::fs::remove_dir_all(c.data_dir("dev")).unwrap();
        assert!(apply_error(&p, &c, false).contains("terraform data dir is missing"));
    }

    #[test]
    fn refresh_destroy_output_fmt_import_state_tf() {
        let (p, c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{}'\nexit 0");
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        let flags = Flags {
            targets: strings(&["t"]),
            ..Flags::default()
        };
        do_targeted(&c, &mut ctx, "apply", &flags).unwrap();
        do_targeted(&c, &mut ctx, "destroy", &Flags::default()).unwrap();
        do_output(&c, &mut ctx, &strings(&["x"])).unwrap();
        do_fmt(&c, &mut ctx, &strings(&["-check"])).unwrap();
        do_import(&c, &mut ctx, "a.b", "id-1", &[]).unwrap();
        do_state(&c, &mut ctx, "list", &strings(&["a"]), &strings(&["-x"])).unwrap();
        do_state(&c, &mut ctx, "mv", &strings(&["a", "b"]), &[]).unwrap();
        do_tf(&c, &mut ctx, &strings(&["validate"])).unwrap();
        let tfvars = p.path("tfvars/dev.tfvars").display().to_string();
        let chdir = format!("-chdir={}", p.path("terraform/demo").display());
        assert_eq!(
            p.terraform_calls(),
            [
                format!("{chdir} apply -var-file={tfvars} -target=t"),
                format!("{chdir} destroy -var-file={tfvars}"),
                format!("{chdir} output -json x"),
                format!(
                    "fmt -recursive -check {} {}",
                    p.path("terraform").display(),
                    p.path("tfvars").display()
                ),
                format!("{chdir} state pull"),
                format!("{chdir} import -var-file={tfvars} a.b id-1"),
                format!("{chdir} state list -x a"),
                format!("{chdir} state pull"),
                format!("{chdir} state mv a b"),
                format!("{chdir} validate"),
            ]
        );
        assert_eq!(std::fs::read_dir(c.snapshots_dir("dev")).unwrap().count(), 2);
    }

    #[test]
    fn tf_without_an_active_slot_runs_bare() {
        let p = Project::new("");
        p.fake_terraform("");
        let c = p.config();
        do_tf(&c, &mut Streams::default().ctx(&p.root), &strings(&["version"])).unwrap();
        assert_eq!(p.terraform_calls(), ["version"]);
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.slot_env = Some("dev".into());
        assert!(
            do_tf(&c, &mut ctx, &[])
                .unwrap_err()
                .to_string()
                .starts_with("no tfvars defines slot \"dev\"")
        );
        p.tfvars("dev", DEMO);
        assert!(
            do_tf(&c, &mut ctx, &[])
                .unwrap_err()
                .to_string()
                .starts_with("No state for slot \"dev\"")
        );
    }

    #[test]
    fn fmt_needs_a_directory() {
        let p = Project::new("terraform_dir = \"nope\"\ntfvars_dir = \"nope2\"");
        let err = do_fmt(&p.config(), &mut Streams::default().ctx(&p.root), &[]).unwrap_err();
        assert!(err.to_string().starts_with("Neither "));
    }

    #[test]
    fn a_slot_whose_banner_breaks_is_an_error_not_a_fallback() {
        let (p, c) = bound("");
        p.write("x.tfvars", "no banner\n");
        let state = load_state(&c, "dev").unwrap().unwrap();
        save_state(
            &c,
            &MyState {
                slot: "x.tfvars".to_owned(),
                ..state
            },
        )
        .unwrap();
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.slot_flag = Some("x.tfvars".into());
        assert!(
            matches!(do_output(&c, &mut ctx, &[]), Err(Error::Msg(m)) if m.starts_with("Project info banner not found"))
        );
    }

    #[test]
    fn rm() {
        let (p, c) = bound("");
        let mut s = Streams::default();
        let ctx = s.ctx(&p.root);
        assert_eq!(
            do_rm(&c, &ctx, "dev", false).unwrap_err().to_string(),
            "slot \"dev\" is the active slot (file). Pass --force to remove it anyway."
        );
        assert!(
            do_rm(&c, &ctx, "qa", false)
                .unwrap_err()
                .to_string()
                .starts_with("no slot directory at ")
        );
        assert_eq!(do_rm(&c, &ctx, "dev", true).unwrap(), c.slot_dir("dev"));
    }

    #[test]
    fn status_and_listing() {
        let (p, c) = bound("");
        p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        let mut s = Streams::default();
        let ctx = s.ctx(&p.root);
        let report = status_report(&c, &ctx).unwrap().unwrap();
        assert_eq!((report.active.name.as_str(), report.plan_ready), ("dev", false));
        let listing = slot_listing(&c, &ctx).unwrap();
        assert_eq!(
            listing
                .iter()
                .map(|l| (l.name.as_str(), l.warm, l.is_active))
                .collect::<Vec<_>>(),
            [("dev", true, true), ("qa", false, false)]
        );
        assert!(listing[0].warm_mtime.as_ref().unwrap().ends_with("+00:00"));
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.slot_flag = Some("qa".into());
        assert_eq!(status_report(&c, &ctx), Ok(None));
        let none = Project::new("");
        assert_eq!(status_report(&none.config(), &ctx), Ok(None));
        ctx.slot_flag = None;
        assert_eq!(status_report(&none.config(), &ctx), Ok(None));
    }

    #[test]
    fn confirming_a_protected_slot() {
        let check = |input: &str, setup: &dyn Fn(&mut Ctx)| {
            let mut s = Streams::with_input(input);
            let mut ctx = s.ctx(Path::new("/"));
            setup(&mut ctx);
            let result = confirm_protected(&mut ctx, "prod", true, "destroy").map_err(|e| e.to_string());
            (result, s.err())
        };
        let interactive = |ctx: &mut Ctx| ctx.interactive = true;
        assert_eq!(
            check("prod\n", &interactive),
            (
                Ok(()),
                "slot \"prod\" is protected. Type its name to run `tfp destroy`: ".into()
            )
        );
        for typed in ["y\n", "yes\n", "Prod\n", ""] {
            assert_eq!(
                check(typed, &interactive).0,
                Err("Aborted: that is not \"prod\".".into())
            );
        }
        // At a terminal, TFP_CONFIRM does not stand in for typing.
        let exported = |ctx: &mut Ctx| {
            ctx.interactive = true;
            ctx.confirm_env = Some("prod".into());
        };
        assert!(check("", &exported).0.is_err());
        // Without one, only TFP_CONFIRM naming the slot will do.
        let (refused, prompt) = check("prod\n", &|_| {});
        assert!(
            refused
                .unwrap_err()
                .ends_with("Set TFP_CONFIRM=prod to allow `tfp destroy`.")
        );
        assert_eq!(prompt, "");
        assert_eq!(
            check("", &|ctx| ctx.confirm_env = Some("dev".into())).0,
            Err("slot \"prod\" is protected and TFP_CONFIRM=\"dev\" does not name it. Refusing `tfp destroy`.".into())
        );
        assert_eq!(check("", &|ctx| ctx.confirm_env = Some("prod".into())).0, Ok(()));
        assert_eq!(check("", &|ctx| ctx.terraform.dry_run = true), (Ok(()), String::new()));
        let mut s = Streams::default();
        assert_eq!(
            confirm_protected(&mut s.ctx(Path::new("/")), "dev", false, "apply"),
            Ok(())
        );
    }

    const PROTECTED: &str =
        r#"{"header":"terraform","project":"demo","state_key":"/demo/dev.tfstate","protected":true}"#;

    #[test]
    fn protected_slots_refuse_every_mutating_command() {
        let (p, c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{}'\nexit 0");
        p.tfvars("dev", PROTECTED);
        do_plan(&c, &mut Streams::default().ctx(&p.root), &Flags::default()).unwrap();
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        let refused = |result: Result<()>| matches!(result, Err(Error::Msg(m)) if m.contains("Set TFP_CONFIRM=dev"));
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        // `--force` is about stale plans, not protection.
        assert!(refused(do_apply(&c, &mut ctx, true, &[])));
        assert!(refused(do_apply(&c, &mut ctx, false, &[])));
        assert!(refused(do_targeted(&c, &mut ctx, "apply", &Flags::default())));
        assert!(refused(do_targeted(&c, &mut ctx, "destroy", &Flags::default())));
        assert!(refused(do_import(&c, &mut ctx, "a.b", "id", &[])));
        for sub in ["mv", "rm", "push", "replace-provider"] {
            assert!(
                refused(do_state(&c, &mut ctx, sub, &strings(&["a", "b"]), &[])),
                "{sub}"
            );
        }
        assert_eq!(p.terraform_calls().len(), 1, "only the plan ran");
        // Reading is unaffected.
        do_state(&c, &mut ctx, "list", &[], &[]).unwrap();
        do_output(&c, &mut ctx, &[]).unwrap();
        // TFP_CONFIRM naming the slot lets them through.
        ctx.confirm_env = Some("dev".into());
        do_targeted(&c, &mut ctx, "destroy", &Flags::default()).unwrap();
        do_apply(&c, &mut ctx, false, &[]).unwrap();
        assert!(p.terraform_calls().last().unwrap().contains(" apply -var-file="));
    }

    /// `tfp apply --force` on a protected slot, typing its name, where `terraform show` runs `show`: what was asked.
    fn protected_apply_prompt(show: &str) -> String {
        let (p, c) = bound(&format!("[ \"$2\" = show ] && {{ {show}; }}\nexit 0"));
        p.tfvars("dev", PROTECTED);
        do_plan(&c, &mut Streams::default().ctx(&p.root), &Flags::default()).unwrap();
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        let mut s = Streams::with_input("dev\n");
        let mut ctx = s.ctx(&p.root);
        ctx.interactive = true;
        do_apply(&c, &mut ctx, true, &[]).unwrap();
        let calls = p.terraform_calls();
        let plan = c.tfplan_file("dev").display().to_string();
        assert!(calls[1].ends_with(&format!(" show -json {plan}")), "{calls:?}");
        assert!(calls[2].contains(" apply -var-file="), "{calls:?}");
        s.err()
    }

    #[test]
    fn protected_apply_shows_what_the_plan_removes() {
        let show = r#"{"format_version":"1.2","resource_changes":[
            {"address":"a.new","change":{"actions":["create"]}},
            {"address":"a.edit","change":{"actions":["update"]}},
            {"address":"a.same","change":{"actions":["no-op"]}},
            {"address":"a.gone","change":{"actions":["delete"]}},
            {"address":"a.cbd","change":{"actions":["create","delete"]}},
            {"address":"a.dbc","change":{"actions":["delete","create"]}}]}"#;
        assert_eq!(
            protected_apply_prompt(&format!("echo '{show}'")),
            "slot \"dev\" is protected. This plan adds 1, changes 1, destroys 1, replaces 2:\n  \
             - a.gone   (destroy)\n  - a.cbd   (replace)\n  - a.dbc   (replace)\n\
             Type its name to run `tfp apply`: "
        );
        assert_eq!(
            protected_apply_prompt("echo '{}'"),
            "slot \"dev\" is protected. This plan adds 0, changes 0, destroys 0, replaces 0.\n\
             Type its name to run `tfp apply`: "
        );
        // Without a summary, it still asks.
        for broken in ["exit 1", "echo 'not json'"] {
            assert_eq!(
                protected_apply_prompt(broken),
                "slot \"dev\" is protected. warning: plan summary unavailable (`terraform show -json` failed).\n\
                 Type its name to run `tfp apply`: "
            );
        }
    }

    #[test]
    fn drift_skips_and_errors_and_leaves_saved_plans_alone() {
        let (p, c) = bound("[ \"$2\" = plan ] && exit 2\nexit 0");
        std::fs::write(c.tfplan_file("dev"), "saved").unwrap();
        std::fs::write(c.tfplan_meta("dev"), "meta").unwrap();
        p.tfvars("prod", r#"{"header":"terraform","project":"demo","protected":true}"#);
        for slot in ["nodata", "secret", "zz"] {
            p.tfvars(slot, r#"{"header":"terraform","project":"demo"}"#);
            do_use(&c, &mut Streams::default().ctx(&p.root), slot, &[]).unwrap();
        }
        write_active(&c, "dev").unwrap();
        std::fs::remove_dir_all(c.data_dir("nodata")).unwrap();
        p.write(
            "tfvars/secret.tfvars",
            "# {\"header\":\"terraform\",\"project\":\"demo\"}\nx = \"{{ vault://k }}\"\n",
        );
        std::fs::write(c.state_file("zz"), "not json").unwrap();
        std::fs::remove_file(p.path("terraform.log")).unwrap();
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.terraform.last_invocation = Some(c.last_invocation("dev"));
        let entries = do_drift(&c, &mut ctx);
        assert_eq!(ctx.terraform.last_invocation, Some(c.last_invocation("dev")));
        let described: Vec<(String, String)> = entries.iter().map(|e| (e.slot.clone(), describe_drift(e))).collect();
        assert_eq!(described[0], ("dev".into(), "drift".into()));
        assert!(
            described[1]
                .1
                .starts_with("error: slot \"nodata\": terraform data dir is missing"),
            "{described:?}"
        );
        assert_eq!(described[2], ("prod".into(), "skipped: protected".into()));
        assert!(
            described[3].1.starts_with("error: ") && described[3].1.contains("vault://"),
            "{described:?}"
        );
        assert!(
            described[4].1.starts_with("error: ") && described[4].1.contains("state.json"),
            "{described:?}"
        );
        assert_eq!(std::fs::read_to_string(c.tfplan_file("dev")).unwrap(), "saved");
        assert_eq!(std::fs::read_to_string(c.tfplan_meta("dev")).unwrap(), "meta");
        assert_eq!(crate::state::read_active(&c).unwrap().as_deref(), Some("dev"));
        let calls = p.terraform_calls();
        assert_eq!(calls.len(), 1, "{calls:?}");
        assert!(calls[0].ends_with(" -detailed-exitcode -lock=false -input=false") && !calls[0].contains("-out="));
        assert!(c.last_invocation("dev").exists());
    }

    #[test]
    fn status_and_listing_show_protection() {
        let (p, c) = bound("");
        let mut s = Streams::default();
        let ctx = s.ctx(&p.root);
        assert!(!status_report(&c, &ctx).unwrap().unwrap().protected);
        p.tfvars("dev", PROTECTED);
        assert!(status_report(&c, &ctx).unwrap().unwrap().protected);
        assert!(slot_listing(&c, &ctx).unwrap()[0].protected);
    }

    #[test]
    fn trace_builders() {
        let (_p, c) = bound("");
        let state = load_state(&c, "dev").unwrap().unwrap();
        let lock = build_providers_lock_argv(&c, &state.source_root, &strings(&["a_b", "a_b", "c_d"]));
        assert_eq!(lock[4..], strings(&["-platform=a_b", "-platform=c_d"]));
    }
}
