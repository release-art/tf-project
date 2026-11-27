//! The command line: parsing, dispatch, what each command prints, and exit codes.

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, CommandFactory, FromArgMatches, Parser, Subcommand};

use crate::commands::{self, Flags};
use crate::config::Config;
use crate::self_commands::{self as sc, BreakResult, TraceCommand};
use crate::text::{shell_join, utc};
use crate::{Ctx, Error, Result, say, slots};

/// Terraform / OpenTofu wrapper with named per-tfvars slots, cached `.terraform/`, secret injection and apply-safety
/// guards. `tfp tf ...` runs any other Terraform command in the active slot.
#[derive(Debug, Parser)]
#[command(name = "tf-project", disable_version_flag = true)]
struct Cli {
    /// Operate against this slot for this invocation (does not change the saved active slot).
    #[arg(short, long)]
    slot: Option<String>,
    /// Show version and exit.
    #[arg(long)]
    version: bool,
    /// Echo each Terraform command line to stderr before running it. Accepted anywhere before `--`.
    #[arg(long)]
    verbose: bool,
    /// Print each Terraform command line and run nothing. Accepted anywhere before `--`.
    #[arg(long)]
    dry_run: bool,
    #[command(subcommand)]
    command: Option<Cmd>,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    #[command(flatten)]
    Wrapped(Wrapped),
    /// Manage tf-project itself.
    #[command(name = "self", subcommand)]
    Self_(SelfCmd),
}

/// Arguments after `--` go to Terraform verbatim. Unknown `-flags` anywhere are moved there too.
#[derive(Debug, Args)]
struct Extra {
    /// Passed to Terraform as given.
    #[arg(last = true, value_name = "TERRAFORM_ARGS")]
    extra: Vec<String>,
}

#[derive(Debug, Args)]
struct Targets {
    /// Limit to a resource address (repeatable).
    #[arg(short = 't', long = "target", value_name = "RESOURCE")]
    targets: Vec<String>,
}

#[derive(Debug, Subcommand)]
enum Wrapped {
    /// Switch to a slot: a slot name (banner `slot`, or tfvars stem) or a tfvars path. Runs `terraform init` only
    /// when the slot was never initialised or its backend changed.
    Use {
        /// Slot name or tfvars path.
        target: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// Re-read the active slot's banner and re-bind its backend (`terraform init -reconfigure`). Run this after
    /// editing a banner's `state_key` / `backend_config`. Does not change the active slot.
    Init(Extra),
    /// List all slots derived from tfvars banners.
    Ls {
        /// Emit JSON instead of a table.
        #[arg(long)]
        json: bool,
    },
    /// Remove a slot directory (does not touch the tfvars file).
    Rm {
        /// The slot.
        slot: String,
        /// Allow removing the active slot.
        #[arg(long)]
        force: bool,
    },
    /// Run `terraform plan` against the active slot, saving the plan.
    Plan {
        #[command(flatten)]
        targets: Targets,
        /// Force replacement of a resource (repeatable).
        #[arg(short = 'r', long = "replace", value_name = "RESOURCE")]
        replaces: Vec<String>,
        #[command(flatten)]
        extra: Extra,
    },
    /// Apply the saved plan.
    Apply {
        /// Apply even if the plan's inputs changed.
        #[arg(long)]
        force: bool,
        #[command(flatten)]
        extra: Extra,
    },
    /// Apply directly (without a saved plan), optionally targeted.
    Refresh {
        #[command(flatten)]
        targets: Targets,
        #[command(flatten)]
        extra: Extra,
    },
    /// Run `terraform destroy`, optionally targeted.
    Destroy {
        #[command(flatten)]
        targets: Targets,
        #[command(flatten)]
        extra: Extra,
    },
    /// Recursively `terraform fmt` the terraform and tfvars directories.
    Fmt(Extra),
    /// Print `terraform output -json`.
    Output(Extra),
    /// Import an existing resource into the state, with the decrypted tfvars and the slot's environment.
    Import {
        /// Target resource address.
        address: String,
        /// Provider-specific resource ID.
        id: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// Terraform state subcommands.
    #[command(subcommand)]
    State(StateCmd),
    /// Print the last Terraform invocation for the active slot.
    Last,
    /// Summary of the active slot.
    Status,
    /// Check every initialised, unprotected slot for drift with `terraform plan -detailed-exitcode`. Exits 0 when
    /// all are clean, 2 when any drifted, 1 when any failed. Saves no plan and leaves the active slot alone.
    Drift {
        /// Emit the summary as JSON (Terraform's output goes to stderr).
        #[arg(long)]
        json: bool,
    },
    /// Run any Terraform command in the active slot: `tfp tf validate`, `tfp tf providers`, ...
    Tf {
        /// Passed to Terraform as given.
        #[arg(trailing_var_arg = true, allow_hyphen_values = true, value_name = "TERRAFORM_ARGS")]
        args: Vec<String>,
    },
}

#[derive(Debug, Subcommand)]
enum StateCmd {
    /// List resources in the state.
    List {
        /// Resource addresses to filter on.
        addresses: Vec<String>,
        #[command(flatten)]
        extra: Extra,
    },
    /// Show a resource in the state.
    Show {
        /// Resource address.
        address: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// Move an item in the state (snapshots the remote state first).
    Mv {
        /// Source address.
        source: String,
        /// Destination address.
        destination: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// Remove instances from the state (snapshots the remote state first).
    Rm {
        /// Resource addresses.
        #[arg(required = true)]
        addresses: Vec<String>,
        #[command(flatten)]
        extra: Extra,
    },
    /// Pull the current state to stdout.
    Pull(Extra),
    /// Update the remote state from a local file.
    Push {
        /// Local state file.
        path: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// Replace a provider in the state (snapshots the remote state first).
    ReplaceProvider {
        /// Current provider address.
        from: String,
        /// Replacement provider address.
        to: String,
        #[command(flatten)]
        extra: Extra,
    },
    /// List identities of resources in the state.
    Identities {
        /// Resource addresses to filter on.
        addresses: Vec<String>,
        #[command(flatten)]
        extra: Extra,
    },
}

#[derive(Debug, Subcommand)]
enum SelfCmd {
    /// Write a starter `tf_project` config in the current directory.
    Init,
    /// Inspect the configuration.
    #[command(subcommand)]
    Config(ConfigCmd),
    #[command(flatten)]
    WithConfig(SelfWithConfig),
}

#[derive(Debug, Subcommand)]
enum SelfWithConfig {
    /// Inspect or reset saved per-slot state.
    #[command(subcommand)]
    State(SelfStateCmd),
    /// Run environment sanity checks.
    Doctor,
    /// Print the command line a wrapped command would run, without running anything.
    Trace {
        /// The wrapped command.
        command: TraceCommand,
        #[command(flatten)]
        targets: Targets,
        /// Force replacement of a resource (repeatable).
        #[arg(short = 'r', long = "replace", value_name = "RESOURCE")]
        replaces: Vec<String>,
    },
    /// Inspect tfvars banners.
    #[command(subcommand)]
    Banner(BannerCmd),
    /// Remote-tfstate snapshots (saved automatically before state-changing commands).
    #[command(subcommand)]
    Snapshot(SnapshotCmd),
    /// Provider tooling.
    #[command(subcommand)]
    Providers(ProvidersCmd),
    /// Inspect or break the remote-state lock (azurerm, or s3 with DynamoDB).
    #[command(subcommand)]
    Lock(LockCmd),
}

#[derive(Debug, Subcommand)]
enum ConfigCmd {
    /// Print the effective configuration.
    Print {
        /// Emit JSON.
        #[arg(long)]
        json: bool,
    },
    /// Print the config file in use.
    Path,
}

#[derive(Debug, Subcommand)]
enum SelfStateCmd {
    /// Print the saved slot state as JSON.
    Show {
        /// Every slot.
        #[arg(long)]
        all: bool,
    },
    /// Delete the saved slot state (active slot, or all).
    Clear {
        /// Every slot.
        #[arg(long)]
        all: bool,
    },
}

#[derive(Debug, Subcommand)]
enum BannerCmd {
    /// Parse and validate a tfvars banner.
    Check {
        /// The tfvars file.
        tfvars: PathBuf,
    },
}

#[derive(Debug, Subcommand)]
enum SnapshotCmd {
    /// Pull the remote tfstate to a local file.
    Create {
        /// Output path; defaults to the slot's snapshots directory.
        #[arg(long)]
        dest: Option<PathBuf>,
    },
    /// List the slot's snapshots, newest first.
    List,
    /// Push a snapshot back to the remote backend (`terraform state push -force`).
    Restore {
        /// Snapshot file name or stem; the newest by default.
        name: Option<String>,
        /// Skip the confirmation prompt.
        #[arg(short, long)]
        yes: bool,
    },
}

#[derive(Debug, Subcommand)]
enum ProvidersCmd {
    /// Run `terraform providers lock` for linux amd64 + arm64, the host, and `extra_lock_platforms`.
    Lock {
        /// Lock these platforms instead (repeatable).
        #[arg(short, long = "platform")]
        platforms: Vec<String>,
    },
}

#[derive(Debug, Clone, Copy, Subcommand)]
enum LockCmd {
    /// Show the remote-state lock. Exits 2 when it is held.
    Status,
    /// Release the remote-state lock: `terraform force-unlock` when possible, else break it at the backend.
    Break {
        /// Skip the confirmation prompt.
        #[arg(short, long)]
        yes: bool,
        /// Skip `terraform force-unlock`; break the lease at the backend.
        #[arg(long)]
        blunt: bool,
    },
}

/// Both programs' `main`: run the process's command line and exit with its code.
pub fn main() -> ExitCode {
    // Ctrl-C reaches Terraform through the terminal; tfp keeps waiting for it to stop cleanly.
    let _ = ctrlc::set_handler(|| {});
    let args = std::env::args_os().map(|a| a.to_string_lossy().into_owned()).collect();
    let (mut out, mut err, mut input) = (std::io::stdout(), std::io::stderr(), std::io::stdin().lock());
    let mut ctx = Ctx {
        out: &mut out,
        err: &mut err,
        input: &mut input,
        cwd: std::env::current_dir().unwrap_or_default(),
        slot_flag: None,
        slot_env: std::env::var("TFP_SLOT").ok(),
        confirm_env: std::env::var("TFP_CONFIRM").ok(),
        interactive: std::io::IsTerminal::is_terminal(&std::io::stdin()),
        terraform: crate::terraform::Options::default(),
        search_path: None,
        temp_dir: std::env::temp_dir(),
        http: None,
    };
    ExitCode::from(u8::try_from(run(args, &mut ctx).rem_euclid(256)).unwrap_or(1))
}

/// Run the command line `args` (the program name first) and return the exit code.
pub fn run(args: Vec<String>, ctx: &mut Ctx) -> i32 {
    let GlobalFlags { args, verbose, dry_run } = strip_global_flags(args);
    let name = program_name(&args);
    let command = Cli::command().bin_name(name);
    let parsed = command
        .try_get_matches_from(normalize(args))
        .and_then(|mut m| Cli::from_arg_matches_mut(&mut m));
    let cli = match parsed {
        Ok(cli) => cli,
        Err(e) => return clap_exit(ctx, &e),
    };
    ctx.slot_flag = cli.slot;
    ctx.terraform.verbose = verbose;
    ctx.terraform.dry_run = dry_run;
    let result = match cli.command {
        _ if cli.version => {
            say!(ctx.out, "{}", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        None => {
            let _ = write!(ctx.err, "{}", Cli::command().render_help());
            Ok(2)
        }
        Some(Cmd::Self_(SelfCmd::Init)) => sc::do_self_init(&ctx.cwd)
            .map(|path| report(ctx, &format!("Wrote tf_project config to {}", path.display()))),
        Some(Cmd::Self_(SelfCmd::Config(ConfigCmd::Path))) => {
            sc::do_self_config_path(&ctx.cwd).map(|path| report(ctx, &path.display().to_string()))
        }
        Some(Cmd::Self_(SelfCmd::Config(ConfigCmd::Print { json }))) => with_config(ctx, |config, ctx| {
            let text = if json {
                pretty(&sc::config_json(config))
            } else {
                sc::config_text(config)
            };
            Ok(report(ctx, &text))
        }),
        Some(Cmd::Self_(SelfCmd::WithConfig(command))) => with_config(ctx, |c, ctx| self_command(c, ctx, command)),
        Some(Cmd::Wrapped(command)) => with_config(ctx, |c, ctx| dispatch(c, ctx, command)),
    };
    match result {
        Ok(code) | Err(Error::Exit(code)) => code,
        Err(e) => {
            say!(ctx.err, "{e}");
            1
        }
    }
}

/// Load the config, point `tfp last` recording at the slot the command runs against, and run `f`.
fn with_config(ctx: &mut Ctx, f: impl FnOnce(&Config, &mut Ctx) -> Result<i32>) -> Result<i32> {
    let config = Config::discover(&ctx.cwd)?;
    let active = slots::resolve_active(&config, ctx.slot_flag.as_deref(), ctx.slot_env.as_deref());
    ctx.terraform.last_invocation = active.ok().flatten().map(|a| config.last_invocation(&a.name));
    f(&config, ctx)
}

/// Print `line` and succeed.
fn report(ctx: &mut Ctx, line: &str) -> i32 {
    say!(ctx.out, "{line}");
    0
}

fn clap_exit(ctx: &mut Ctx, e: &clap::Error) -> i32 {
    let stream: &mut dyn Write = if e.use_stderr() { &mut *ctx.err } else { &mut *ctx.out };
    let _ = write!(stream, "{}", e.render());
    e.exit_code()
}

fn program_name(args: &[String]) -> String {
    crate::banner::stem(std::path::Path::new(args.first().map_or("tf-project", String::as_str)))
}

/// The command line without `--verbose` and `--dry-run`, and whether each was there.
#[derive(Debug, PartialEq, Eq)]
struct GlobalFlags {
    /// The rest of the command line.
    args: Vec<String>,
    /// `--verbose`.
    verbose: bool,
    /// `--dry-run`.
    dry_run: bool,
}

/// Take `--verbose` and `--dry-run` out of `args`, wherever they are before a `--`.
fn strip_global_flags(args: Vec<String>) -> GlobalFlags {
    let (mut verbose, mut dry_run, mut seen_dash_dash) = (false, false, false);
    let mut out = Vec::with_capacity(args.len());
    for arg in args {
        match arg.as_str() {
            "--" => seen_dash_dash = true,
            "--verbose" if !seen_dash_dash => {
                verbose = true;
                continue;
            }
            "--dry-run" if !seen_dash_dash => {
                dry_run = true;
                continue;
            }
            _ => {}
        }
        out.push(arg);
    }
    GlobalFlags {
        args: out,
        verbose,
        dry_run,
    }
}

/// Let Terraform's own flags sit anywhere after a wrapped command, as they would on `terraform`'s command line: the
/// command's own options and positionals stay where clap reads them, and everything else moves behind `--`, into its
/// passthrough arguments. Anything clap should report as an error is left as it is.
fn normalize(args: Vec<String>) -> Vec<String> {
    let mut cli = Cli::command();
    cli.build();
    let mut cmd = &cli;
    let mut i = 1;
    // The subcommand path, and the options before it.
    while cmd.has_subcommands() {
        let Some(arg) = args.get(i) else { return args };
        if let Some(sub) = cmd.find_subcommand(arg) {
            cmd = sub;
        } else if arg.starts_with('-') {
            i += usize::from(option_takes_value(cmd, arg).unwrap_or(false));
        } else {
            return args;
        }
        i += 1;
    }
    if !cmd.get_arguments().any(|a| a.get_id() == "extra") {
        return args;
    }
    let positionals: Vec<&clap::Arg> = cmd.get_positionals().filter(|a| a.get_id() != "extra").collect();
    let room = if positionals
        .iter()
        .any(|a| a.get_num_args().is_some_and(|n| n.max_values() > 1))
    {
        usize::MAX
    } else {
        positionals.len()
    };
    let (mut own, mut values, mut extra) = (Vec::new(), Vec::new(), Vec::new());
    let mut rest = args.get(i..).unwrap_or_default().iter();
    while let Some(arg) = rest.next() {
        if arg == "--" {
            extra.extend(rest.by_ref().cloned());
        } else if let Some(takes_value) = option_takes_value(cmd, arg) {
            own.push(arg.clone());
            if takes_value {
                own.extend(rest.next().cloned());
            }
        } else if arg.len() > 1 && arg.starts_with('-') || values.len() >= room {
            extra.push(arg.clone());
        } else {
            values.push(arg.clone());
        }
    }
    let mut out: Vec<String> = args.into_iter().take(i).collect();
    out.extend(own);
    out.extend(values);
    if !extra.is_empty() {
        out.push("--".into());
        out.extend(extra);
    }
    out
}

/// Whether `arg` is one of `cmd`'s options, and if so whether a separate value follows it.
fn option_takes_value(cmd: &clap::Command, arg: &str) -> Option<bool> {
    cmd.get_arguments().find_map(|a| {
        let takes = a.get_action().takes_values();
        let long = a
            .get_long()
            .is_some_and(|l| arg == format!("--{l}") || arg.starts_with(&format!("--{l}=")));
        let short = a.get_short().is_some_and(|s| arg == format!("-{s}"));
        (long || short).then_some(takes && !arg.contains('='))
    })
}

fn dispatch(config: &Config, ctx: &mut Ctx, command: Wrapped) -> Result<i32> {
    let flags = |targets: Targets, replaces: Vec<String>, extra: Extra| Flags {
        targets: targets.targets,
        replaces,
        extra: extra.extra,
    };
    match command {
        Wrapped::Use { target, extra } => commands::do_use(config, ctx, &target, &extra.extra).map(drop),
        Wrapped::Init(extra) => commands::do_init(config, ctx, &extra.extra).map(drop),
        Wrapped::Ls { json } => return ls(config, ctx, json),
        Wrapped::Rm { slot, force } => {
            commands::do_rm(config, ctx, &slot, force).map(|dir| say!(ctx.out, "removed {}", dir.display()))
        }
        Wrapped::Plan {
            targets,
            replaces,
            extra,
        } => commands::do_plan(config, ctx, &flags(targets, replaces, extra)),
        Wrapped::Apply { force, extra } => commands::do_apply(config, ctx, force, &extra.extra),
        Wrapped::Refresh { targets, extra } => {
            commands::do_targeted(config, ctx, "apply", &flags(targets, vec![], extra))
        }
        Wrapped::Destroy { targets, extra } => {
            commands::do_targeted(config, ctx, "destroy", &flags(targets, vec![], extra))
        }
        Wrapped::Fmt(extra) => commands::do_fmt(config, ctx, &extra.extra),
        Wrapped::Output(extra) => commands::require_active(config, ctx).and_then(|_| {
            let args = [&["output".to_owned(), "-json".to_owned()], &extra.extra[..]].concat();
            commands::do_tf(config, ctx, &args)
        }),
        Wrapped::Import { address, id, extra } => commands::do_import(config, ctx, &address, &id, &extra.extra),
        Wrapped::State(state) => {
            let (sub, args, extra) = match state {
                StateCmd::List { addresses, extra } => ("list", addresses, extra),
                StateCmd::Show { address, extra } => ("show", vec![address], extra),
                StateCmd::Mv {
                    source,
                    destination,
                    extra,
                } => ("mv", vec![source, destination], extra),
                StateCmd::Rm { addresses, extra } => ("rm", addresses, extra),
                StateCmd::Pull(extra) => ("pull", vec![], extra),
                StateCmd::Push { path, extra } => ("push", vec![path], extra),
                StateCmd::ReplaceProvider { from, to, extra } => ("replace-provider", vec![from, to], extra),
                StateCmd::Identities { addresses, extra } => ("identities", addresses, extra),
            };
            commands::do_state(config, ctx, sub, &args, &extra.extra)
        }
        Wrapped::Last => sc::do_last_invocation(config, ctx).map(|last| {
            say!(ctx.out, "timestamp = {}", last.timestamp);
            say!(
                ctx.out,
                "exit_code = {}",
                last.exit_code.map_or_else(|| "(unknown)".into(), |c| c.to_string())
            );
            say!(ctx.out, "argv      = {}", shell_join(&last.argv));
        }),
        Wrapped::Status => return status(config, ctx),
        Wrapped::Drift { json } => return Ok(drift(config, ctx, json)),
        Wrapped::Tf { args } => commands::do_tf(config, ctx, &args),
    }
    .map(|()| 0)
}

fn ls(config: &Config, ctx: &mut Ctx, json: bool) -> Result<i32> {
    let listing = commands::slot_listing(config, ctx)?;
    let orphans = slots::list_orphans(config);
    if let Err(e) = commands::check_slots(config, ctx) {
        say!(ctx.err, "warning: {e}");
    }
    if json {
        let skipped = slots::scan_slots(config).skipped;
        say!(
            ctx.out,
            "{}",
            pretty(&serde_json::json!({"slots": listing, "orphans": orphans, "skipped": skipped}))
        );
        return Ok(0);
    }
    if listing.is_empty() {
        say!(ctx.out, "(no slots found in tfvars_dir)");
    } else {
        let header = ["", "SLOT", "PROJECT", "TFVARS", "BACKEND KEY", "INITIALISED"].map(str::to_owned);
        let rows: Vec<[String; 6]> = std::iter::once(header)
            .chain(listing.into_iter().map(|s| {
                let dash = |v: Option<String>| v.unwrap_or_else(|| "-".into());
                let mark = if s.is_active { "*" } else { " " };
                let name = if s.protected {
                    format!("{} (protected)", s.name)
                } else {
                    s.name
                };
                [
                    mark.into(),
                    name,
                    s.project,
                    s.tfvars.display().to_string(),
                    dash(s.backend_key),
                    dash(s.warm_mtime),
                ]
            }))
            .collect();
        let width = |i: usize| {
            rows.iter()
                .filter_map(|r| r.get(i))
                .map(|v| v.chars().count())
                .max()
                .unwrap_or(0)
        };
        let widths: [usize; 6] = std::array::from_fn(width);
        for row in &rows {
            let cells: Vec<String> = row.iter().zip(widths).map(|(v, w)| format!("{v:<w$}")).collect();
            say!(ctx.out, "{}", cells.join("  ").trim_end());
        }
    }
    if !orphans.is_empty() {
        say!(ctx.out, "\norphans (slot dirs with no matching banner):");
        for o in orphans {
            say!(
                ctx.out,
                "  {}    {}    (run `tfp rm {}` to remove)",
                o.name,
                o.slot_dir.display(),
                o.name
            );
        }
    }
    Ok(0)
}

fn drift(config: &Config, ctx: &mut Ctx, json: bool) -> i32 {
    ctx.terraform.stdout_to_stderr = json;
    let entries = commands::do_drift(config, ctx);
    if json {
        say!(ctx.out, "{}", pretty(&serde_json::json!(entries)));
    } else {
        let width = entries.iter().map(|e| e.slot.chars().count()).max().unwrap_or(0).max(4);
        say!(ctx.out, "{:<width$}  RESULT", "SLOT");
        for e in &entries {
            say!(ctx.out, "{:<width$}  {}", e.slot, commands::describe_drift(e));
        }
    }
    let any = |r| entries.iter().any(|e| e.result == r);
    if any(commands::DriftResult::Error) {
        1
    } else if any(commands::DriftResult::Drift) {
        2
    } else {
        0
    }
}

fn status(config: &Config, ctx: &mut Ctx) -> Result<i32> {
    let Some(report) = commands::status_report(config, ctx)? else {
        say!(ctx.err, "No active slot. Run `tfp use <slot>` first.");
        return Ok(1);
    };
    let state = &report.state;
    let env_keys: Vec<&str> = state.environ.keys().map(String::as_str).collect();
    say!(ctx.out, "slot        = {} (via {})", state.slot, report.active.source);
    say!(ctx.out, "tfvars      = {}", state.tfvars.display());
    say!(ctx.out, "source_root = {}", state.source_root.display());
    say!(
        ctx.out,
        "backend_key = {}",
        state.backend_config.get("key").map_or("(none)", String::as_str)
    );
    say!(
        ctx.out,
        "env keys    = {}",
        if env_keys.is_empty() {
            "(none)".into()
        } else {
            env_keys.join(", ")
        }
    );
    say!(
        ctx.out,
        "plan        = {}",
        if report.plan_ready { "ready" } else { "absent" }
    );
    say!(ctx.out, "protected   = {}", if report.protected { "yes" } else { "no" });
    Ok(0)
}

fn pretty(value: &serde_json::Value) -> String {
    serde_json::to_string_pretty(value).unwrap_or_default()
}

/// Ask before something destructive; no is an abort.
fn confirm(ctx: &mut Ctx, question: &str) -> Result<()> {
    if ctx.confirm(question) {
        return Ok(());
    }
    say!(ctx.err, "Aborted!");
    Err(Error::Exit(1))
}

fn self_command(config: &Config, ctx: &mut Ctx, command: SelfWithConfig) -> Result<i32> {
    match command {
        SelfWithConfig::State(SelfStateCmd::Show { all }) => {
            let value = sc::do_self_state_show(config, ctx, all)?;
            say!(ctx.out, "{}", pretty(&value));
        }
        SelfWithConfig::State(SelfStateCmd::Clear { all }) => {
            let removed = sc::do_self_state_clear(config, ctx, all)?;
            if removed.is_empty() {
                say!(ctx.out, "No state file to remove.");
            }
            for path in removed {
                say!(ctx.out, "Removed {}", path.display());
            }
        }
        SelfWithConfig::Doctor => {
            let checks = sc::do_self_doctor(config, ctx.search_path.as_deref());
            let width = checks.iter().map(|c| c.name.chars().count()).max().unwrap_or(0);
            for c in &checks {
                say!(
                    ctx.out,
                    "[{}] {:<width$}  {}",
                    if c.ok { "OK  " } else { "FAIL" },
                    c.name,
                    c.detail
                );
            }
            return Ok(i32::from(checks.iter().any(|c| !c.ok)));
        }
        SelfWithConfig::Trace {
            command,
            targets,
            replaces,
        } => {
            let flags = Flags {
                targets: targets.targets,
                replaces,
                extra: vec![],
            };
            let argv = sc::do_self_trace(config, ctx, command, &flags)?;
            say!(ctx.out, "{}", shell_join(&argv));
        }
        SelfWithConfig::Banner(BannerCmd::Check { tfvars }) => {
            let summary = sc::do_self_banner_check(config, ctx, &tfvars)?;
            say!(ctx.out, "{}", pretty(&summary));
        }
        SelfWithConfig::Snapshot(command) => snapshot(config, ctx, command)?,
        SelfWithConfig::Providers(ProvidersCmd::Lock { platforms }) => {
            sc::do_self_providers_lock(config, ctx, &platforms)?;
        }
        SelfWithConfig::Lock(command) => return lock(config, ctx, command),
    }
    Ok(0)
}

fn snapshot(config: &Config, ctx: &mut Ctx, command: SnapshotCmd) -> Result<()> {
    match command {
        SnapshotCmd::Create { dest } => {
            let written = sc::do_self_snapshot(config, ctx, dest.as_deref())?;
            say!(ctx.out, "Wrote snapshot to {}", written.display());
        }
        SnapshotCmd::List => {
            let entries = sc::do_self_snapshot_list(config, ctx)?;
            if entries.is_empty() {
                say!(ctx.out, "(no snapshots)");
            }
            for e in entries {
                let name = e.path.file_name().unwrap_or_default().to_string_lossy();
                say!(
                    ctx.out,
                    "{name}\t{}\t{}\t{}",
                    e.size,
                    utc(e.mtime, false),
                    e.path.display()
                );
            }
        }
        SnapshotCmd::Restore { name, yes } => {
            if !yes {
                let which = name
                    .as_ref()
                    .map_or_else(|| "the newest snapshot".to_owned(), |n| format!("snapshot {n:?}"));
                let slot = commands::require_active(config, ctx)?.name;
                confirm(
                    ctx,
                    &format!("Restore {which} of slot {slot:?}? It OVERWRITES the slot's remote state."),
                )?;
            }
            let restored = sc::do_self_snapshot_restore(config, ctx, name.as_deref())?;
            say!(ctx.out, "Restored {}", restored.display());
        }
    }
    Ok(())
}

fn lock(config: &Config, ctx: &mut Ctx, command: LockCmd) -> Result<i32> {
    match command {
        LockCmd::Status => {
            let status = sc::do_self_lock_status(config, ctx)?;
            say!(ctx.out, "backend        = {}", status.backend);
            say!(ctx.out, "locked         = {}", status.locked);
            say!(ctx.out, "detail         = {}", status.detail);
            let info = &status.info;
            if let Some(id) = &info.id {
                let or_unknown = |v: &Option<String>| v.clone().unwrap_or_else(|| "(unknown)".into());
                say!(ctx.out, "lock_id        = {id}");
                say!(ctx.out, "lock_who       = {}", or_unknown(&info.who));
                say!(ctx.out, "lock_operation = {}", or_unknown(&info.operation));
                say!(ctx.out, "lock_created   = {}", or_unknown(&info.created));
                say!(ctx.out, "\nTo release via terraform: tfp tf force-unlock {id}");
            }
            return Ok(if status.locked { 2 } else { 0 });
        }
        LockCmd::Break { yes, blunt } => {
            if !yes {
                let slot = commands::require_active(config, ctx)?.name;
                confirm(
                    ctx,
                    &format!("Release the state lock of slot {slot:?}? Only if no Terraform is still running on it."),
                )?;
            }
            match sc::do_self_lock_break(config, ctx, blunt)? {
                BreakResult::ForceUnlock(id) => say!(ctx.out, "Released via terraform force-unlock (lock_id={id})."),
                BreakResult::LeaseBreak(backend) => say!(ctx.out, "Broke the {backend} lock at the backend level."),
            }
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::{GlobalFlags, normalize, run, strip_global_flags};
    use crate::commands::tests::{DEMO, bound};
    use crate::testing::{Project, Streams};

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|s| (*s).to_owned()).collect()
    }

    /// Run `tfp <args>` in the project with `input` on stdin: exit code, stdout, stderr.
    fn tfp_in(root: &std::path::Path, args: &[&str], input: &str) -> (i32, String, String) {
        let mut s = Streams::with_input(input);
        let mut cmdline = strings(&["tfp"]);
        cmdline.extend(strings(args));
        let code = run(cmdline, &mut s.ctx(root));
        (code, s.out(), s.err())
    }

    fn tfp(p: &Project, args: &[&str]) -> (i32, String, String) {
        tfp_in(&p.root, args, "")
    }

    #[test]
    fn global_flags_anywhere_before_dash_dash() {
        let flags = strip_global_flags(strings(&["tfp", "plan", "--verbose", "--", "--dry-run"]));
        assert_eq!(
            flags,
            GlobalFlags {
                args: strings(&["tfp", "plan", "--", "--dry-run"]),
                verbose: true,
                dry_run: false
            }
        );
    }

    #[test]
    fn terraform_flags_move_behind_dash_dash() {
        let n = |args: &[&str]| normalize(strings(args)).join(" ");
        assert_eq!(
            n(&["tfp", "plan", "-t", "a", "-lock=false", "-t", "b"]),
            "tfp plan -t a -t b -- -lock=false"
        );
        assert_eq!(
            n(&["tfp", "-s", "dev", "plan", "--target=c", "-var", "x=1"]),
            "tfp -s dev plan --target=c -- -var x=1"
        );
        assert_eq!(n(&["tfp", "plan", "-target=a"]), "tfp plan -- -target=a");
        assert_eq!(
            n(&["tfp", "import", "-lock=false", "addr", "id", "-no-color"]),
            "tfp import addr id -- -lock=false -no-color"
        );
        assert_eq!(n(&["tfp", "import", "a", "b", "c"]), "tfp import a b -- c");
        assert_eq!(
            n(&["tfp", "state", "rm", "a", "b", "-backup=x"]),
            "tfp state rm a b -- -backup=x"
        );
        assert_eq!(n(&["tfp", "plan", "--", "-t", "x"]), "tfp plan -- -t x");
        assert_eq!(n(&["tfp", "apply", "--force"]), "tfp apply --force");
        assert_eq!(n(&["tfp", "plan", "-t"]), "tfp plan -t");
        // Left alone: commands without passthrough, unknown words, and incomplete paths.
        assert_eq!(n(&["tfp", "ls", "-x"]), "tfp ls -x");
        assert_eq!(n(&["tfp", "nope", "-x"]), "tfp nope -x");
        assert_eq!(n(&["tfp", "state"]), "tfp state");
        assert_eq!(n(&["tfp", "--slot", "x", "self", "lock"]), "tfp --slot x self lock");
    }

    #[test]
    fn version_help_and_usage_errors() {
        let p = Project::new("");
        assert_eq!(
            tfp(&p, &["--version"]),
            (0, format!("{}\n", env!("CARGO_PKG_VERSION")), String::new())
        );
        let (code, out, _) = tfp(&p, &["--help"]);
        assert_eq!(code, 0);
        assert!(out.contains("Terraform") && out.contains("Usage: tfp"), "{out}");
        let (code, _, err) = tfp(&p, &["-s", "dev"]);
        assert!(code == 2 && err.contains("Usage:"));
        assert_eq!(tfp(&p, &["nope"]).0, 2);
        let (code, _, err) = tfp_in(std::path::Path::new("/"), &["status"], "");
        assert!(
            code == 1 && err.starts_with("config error: No tf_project.toml"),
            "{err}"
        );
    }

    #[test]
    fn use_status_ls_last_rm() {
        let (p, _c) = bound("");
        p.tfvars("qa", r#"{"header":"terraform","project":"demo","slot":"qa"}"#);
        std::fs::create_dir_all(p.path("tmp/slots/old")).unwrap();
        let (code, out, _) = tfp(&p, &["status"]);
        assert_eq!(code, 0);
        assert!(out.starts_with("slot        = dev (via file)\n"), "{out}");
        assert!(out.contains("backend_key = demo/dev.tfstate\nenv keys    = (none)\nplan        = absent\n"));
        let (code, out, _) = tfp(&p, &["ls"]);
        assert_eq!(code, 0);
        let lines: Vec<&str> = out.lines().collect();
        assert!(lines[0].starts_with("   SLOT  PROJECT  TFVARS"), "{out}");
        assert!(
            lines[1].starts_with("*  dev   demo") && lines[2].starts_with("   qa    demo"),
            "{out}"
        );
        assert!(lines[2].ends_with("-                 -"), "{out}");
        assert!(out.contains("orphans (slot dirs with no matching banner):\n  old    "));
        let (_, out, _) = tfp(&p, &["ls", "--json"]);
        let json: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(json["slots"][0]["name"], "dev");
        assert_eq!(json["orphans"][0]["name"], "old");
        assert_eq!(tfp(&p, &["--dry-run", "tf", "version"]).0, 0);
        let (code, out, _) = tfp(&p, &["--verbose", "tf", "version"]);
        assert_eq!((code, out.as_str()), (0, ""));
        let (_, out, _) = tfp(&p, &["last"]);
        assert!(
            out.contains("exit_code = 0\nargv      = ") && out.trim_end().ends_with(" version"),
            "{out}"
        );
        assert_eq!(
            tfp(&p, &["rm", "old"]).1,
            format!("removed {}\n", p.path("tmp/slots/old").display())
        );
        assert_eq!(tfp(&p, &["rm", "dev"]).0, 1);
    }

    #[test]
    fn status_and_ls_when_empty() {
        let p = Project::new("");
        assert_eq!(
            tfp(&p, &["status"]),
            (1, String::new(), "No active slot. Run `tfp use <slot>` first.\n".into())
        );
        assert_eq!(tfp(&p, &["ls"]).1, "(no slots found in tfvars_dir)\n");
        p.tfvars("a", DEMO);
        p.tfvars("b", r#"{"header":"terraform","project":"demo","slot":"a"}"#);
        assert!(
            tfp(&p, &["ls"])
                .2
                .starts_with("warning: slot \"a\" is defined by 2 tfvars files")
        );
        let (code, _, err) = tfp(&p, &["last"]);
        assert!(code == 1 && err.starts_with("No active slot."));
    }

    #[test]
    fn wrapped_commands_reach_terraform() {
        let (p, _c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{}'\nexit 0");
        for args in [
            &["plan", "-t", "a", "-lock=false"][..],
            &["refresh", "-t", "b"],
            &["destroy"],
            &["fmt"],
            &["output"],
            &["import", "x.y", "id"],
            &["state", "list", "a"],
            &["state", "show", "a"],
            &["state", "mv", "a", "b"],
            &["state", "rm", "a"],
            &["state", "pull"],
            &["state", "push", "f.tfstate"],
            &["state", "replace-provider", "x", "y"],
            &["state", "identities"],
            &["init", "-upgrade=false"],
            &["use", "dev"],
            &["self", "providers", "lock", "-p", "linux_amd64"],
        ] {
            assert_eq!(tfp(&p, args).0, 0, "{args:?}");
        }
        let calls = p.terraform_calls();
        assert!(
            calls[0].contains(" plan -var-file=") && calls[0].contains("-target=a -lock=false -out="),
            "{calls:?}"
        );
        assert!(calls.iter().any(|c| c.ends_with("state replace-provider x y")));
        assert!(calls.last().unwrap().ends_with("providers lock -platform=linux_amd64"));
        // apply: the fake terraform writes no plan file; make one.
        std::fs::write(p.path("tmp/slots/dev/tfplan"), "").unwrap();
        assert_eq!(tfp(&p, &["apply"]).0, 0);
        let (code, _, err) = tfp(&p, &["apply", "--force"]);
        assert!(code == 1 && err.contains("has no saved plan"));
    }

    #[test]
    fn terraform_exit_codes_pass_through() {
        let (p, _c) = bound("[ \"$2\" = validate ] && exit 3\nexit 0");
        assert_eq!(tfp(&p, &["tf", "validate"]), (3, String::new(), String::new()));
    }

    #[test]
    fn self_commands() {
        let (p, _c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{\"v\":1}'\nexit 0");
        let (code, out, _) = tfp(&p, &["self", "config", "print"]);
        assert_eq!(code, 0);
        assert!(
            out.contains(
                "
secret_sources:
  op = 1Password CLI (op inject)
"
            ) && out.contains("snapshot_retention = 200"),
            "{out}"
        );
        let (_, out, _) = tfp(&p, &["self", "config", "print", "--json"]);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&out).unwrap()["snapshot_retention"],
            200
        );
        assert_eq!(
            tfp(&p, &["self", "config", "path"]).1,
            format!("{}\n", p.path("tf_project.toml").display())
        );
        let (_, out, _) = tfp(&p, &["self", "state", "show"]);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&out).unwrap()["schema_version"],
            1
        );
        let (_, out, _) = tfp(&p, &["self", "state", "show", "--all"]);
        assert!(out.contains("\"dev\": {"));
        let (code, out, _) = tfp(&p, &["self", "doctor"]);
        assert_eq!(code, 0, "{out}");
        assert!(out.starts_with("[OK  ] terraform binary"), "{out}");
        let (_, out, _) = tfp(&p, &["self", "trace", "plan", "-t", "a b"]);
        let words = shlex::split(&out).unwrap();
        assert_eq!(
            words[2..5],
            strings(&["plan", "-var-file=<DECRYPTED_TFVARS>", "-target=a b"])
        );
        let (_, out, err) = tfp(&p, &["self", "banner", "check", "tfvars/dev.tfvars"]);
        assert!(err.is_empty() && out.contains("\"slot\": \"dev\""));
        let (_, out, _) = tfp(&p, &["self", "snapshot", "create"]);
        assert!(
            out.starts_with("Wrote snapshot to ") && out.trim_end().ends_with("-manual.tfstate"),
            "{out}"
        );
        let (_, out, _) = tfp(&p, &["self", "snapshot", "create", "--dest", "out/s.tfstate"]);
        assert_eq!(
            out,
            format!("Wrote snapshot to {}\n", p.path("out/s.tfstate").display())
        );
        let (_, out, _) = tfp(&p, &["self", "snapshot", "list"]);
        assert!(out.contains("-manual.tfstate\t8\t"), "{out}");
        let (code, _, err) = tfp_in(&p.root, &["self", "snapshot", "restore"], "n\n");
        assert_eq!((code, err.ends_with("[y/N]: Aborted!\n")), (1, true), "{err}");
        let (code, out, _) = tfp_in(&p.root, &["self", "snapshot", "restore"], "y\n");
        assert!(code == 0 && out.starts_with("Restored "), "{out}");
        assert!(p.terraform_calls().last().unwrap().contains("state push -force "));
        assert_eq!(
            tfp(&p, &["self", "state", "clear"]).1,
            format!("Removed {}\n", p.path("tmp/slots/dev/state.json").display())
        );
        assert_eq!(
            tfp(&p, &["self", "state", "clear", "--all"]).1,
            "No state file to remove.\n"
        );
        let (code, _, err) = tfp(&p, &["self", "state", "show"]);
        assert!(code == 1 && err.starts_with("Slot \"dev\" isn't initialised"));
    }

    #[test]
    fn protected_slots_show_and_guard() {
        let (p, _c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{}'\nexit 0");
        assert!(tfp(&p, &["status"]).1.ends_with("protected   = no\n"));
        p.tfvars(
            "dev",
            r#"{"header":"terraform","project":"demo","state_key":"/demo/dev.tfstate","protected":true}"#,
        );
        assert!(tfp(&p, &["status"]).1.ends_with("protected   = yes\n"));
        assert!(
            tfp(&p, &["ls"])
                .1
                .lines()
                .nth(1)
                .unwrap()
                .starts_with("*  dev (protected)  demo")
        );
        let (_, out, _) = tfp(&p, &["self", "banner", "check", "tfvars/dev.tfvars"]);
        assert!(out.contains("\"protected\": true"), "{out}");
        assert_eq!(tfp(&p, &["self", "snapshot", "create"]).0, 0);
        // `-y` skips the y/N question, not the protection; `--dry-run` asks nothing.
        let (code, _, err) = tfp(&p, &["self", "snapshot", "restore", "-y"]);
        assert!(
            code == 1 && err.contains("Set TFP_CONFIRM=dev to allow `tfp self snapshot restore`"),
            "{err}"
        );
        assert_eq!(tfp(&p, &["--dry-run", "destroy"]).0, 0);
        let restore = |confirm: &str| {
            let mut s = Streams::default();
            let mut ctx = s.ctx(&p.root);
            ctx.confirm_env = Some(confirm.into());
            run(strings(&["tfp", "self", "snapshot", "restore", "-y"]), &mut ctx)
        };
        assert_eq!(restore("prod"), 1);
        assert_eq!(restore("dev"), 0);
        assert!(p.terraform_calls().last().unwrap().contains("state push -force "));
    }

    #[test]
    fn self_commands_that_fail() {
        let p = Project::new("");
        p.tfvars("dev", r#"{"header":"terraform","project":"demo","extra":1}"#);
        let (_, _, err) = tfp(&p, &["self", "banner", "check", "tfvars/dev.tfvars"]);
        assert!(err.starts_with("warning: banner in "), "{err}");
        assert_eq!(tfp(&p, &["self", "doctor"]).0, 1);
        assert_eq!(tfp(&p, &["-s", "dev", "self", "snapshot", "list"]).0, 1);
        let (code, _, err) = tfp(&p, &["-s", "dev", "self", "snapshot", "restore", "-y"]);
        assert!(code == 1 && err.starts_with("Slot \"dev\" isn't initialised"), "{err}");
        let (code, _, err) = tfp_in(&p.root, &["self", "lock", "break"], "\n");
        assert!(code == 1 && err.starts_with("No active slot"), "{err}");
        let (code, _, err) = tfp_in(&p.root, &["-s", "dev", "self", "lock", "break"], "\n");
        assert!(code == 1 && err.ends_with("Aborted!\n"), "{err}");
        let (code, _, err) = tfp_in(&p.root, &["self", "snapshot", "restore"], "\n");
        assert!(code == 1 && err.starts_with("No active slot"), "{err}");
        assert_eq!(tfp(&p, &["self", "init"]).0, 1);
        let dir = tempfile::tempdir().unwrap();
        let (code, out, _) = tfp_in(dir.path(), &["self", "init"], "");
        assert!(code == 0 && out.starts_with("Wrote tf_project config to "));
        let (code, _, err) = tfp_in(std::path::Path::new("/"), &["self", "config", "path"], "");
        assert!(code == 1 && err.starts_with("config error: No tf_project config found"));
    }

    #[test]
    fn snapshots_list_and_restore_when_empty() {
        let (p, _c) = bound("");
        assert_eq!(tfp(&p, &["self", "snapshot", "list"]).1, "(no snapshots)\n");
        assert!(
            fails(&p, &["self", "snapshot", "restore", "-y"]).starts_with("Slot \"dev\" has no snapshots to restore.")
        );
    }

    /// Every command that works on the active slot.
    const SLOT_COMMANDS: &[&[&str]] = &[
        &["plan"],
        &["apply"],
        &["refresh"],
        &["destroy"],
        &["output"],
        &["import", "a", "b"],
        &["state", "list"],
        &["state", "mv", "a", "b"],
        &["init"],
        &["last"],
        &["self", "state", "show"],
        &["self", "state", "clear"],
        &["self", "trace", "plan"],
        &["self", "trace", "use"],
        &["self", "snapshot", "create"],
        &["self", "snapshot", "list"],
        &["self", "snapshot", "restore", "-y", "x"],
        &["self", "providers", "lock"],
        &["self", "lock", "status"],
        &["self", "lock", "break", "-y"],
    ];

    fn fails(p: &Project, args: &[&str]) -> String {
        let (code, out, err) = tfp(p, args);
        assert_eq!(code, 1, "{args:?}: {out}{err}");
        err
    }

    #[test]
    fn every_slot_command_needs_an_active_slot() {
        let p = Project::new("");
        for args in SLOT_COMMANDS {
            assert!(fails(&p, args).starts_with("No active slot."), "{args:?}");
        }
    }

    #[test]
    fn every_slot_command_refuses_an_unknown_slot() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        for args in SLOT_COMMANDS.iter().filter(|a| !matches!(a, [_, "state", "clear"])) {
            let err = fails(&p, &[&["-s", "ghost"], *args].concat());
            assert!(err.contains("ghost"), "{args:?}: {err}");
        }
        assert_eq!(
            tfp(&p, &["-s", "ghost", "self", "state", "clear"]).1,
            "No state file to remove.\n"
        );
        assert!(fails(&p, &["-s", "ghost", "tf", "version"]).starts_with("no tfvars defines slot"));
        assert!(fails(&p, &["-s", "dev", "self", "trace", "plan"]).starts_with("Slot \"dev\" isn't initialised. Run"));
        assert!(fails(&p, &["use", "ghost"]).starts_with("no tfvars defines slot"));
    }

    #[test]
    fn an_unreadable_active_pointer_fails_cleanly() {
        let p = Project::new("");
        std::fs::create_dir_all(p.path("tmp/active")).unwrap();
        for args in [&["status"][..], &["ls"], &["rm", "x"], &["tf", "version"], &["plan"]] {
            fails(&p, args);
        }
    }

    #[test]
    fn a_corrupt_state_file_fails_cleanly() {
        let (p, c) = bound("");
        std::fs::write(c.state_file("dev"), "[]").unwrap();
        for args in [
            &["plan"][..],
            &["status"],
            &["self", "state", "show"],
            &["self", "state", "show", "--all"],
            &["self", "trace", "plan"],
        ] {
            assert!(fails(&p, args).contains("expected a JSON object"), "{args:?}");
        }
    }

    #[test]
    fn a_missing_data_dir_needs_tfp_use() {
        let (p, c) = bound("");
        std::fs::remove_dir_all(c.data_dir("dev")).unwrap();
        for args in [
            &["plan"][..],
            &["apply"],
            &["refresh"],
            &["destroy"],
            &["output"],
            &["import", "a", "b"],
            &["state", "list"],
            &["tf", "version"],
        ] {
            assert!(
                fails(&p, args).contains("has lost its .terraform directory"),
                "{args:?}"
            );
        }
    }

    #[test]
    fn a_failing_op_stops_the_command() {
        let p = Project::new("[tf_project.programs]\nop = \"bin/op\"");
        chmod(&p.write("bin/op", "#!/bin/sh\nexit 1\n"), 0o755);
        p.write(
            "tfvars/dev.tfvars",
            &format!("# {DEMO}\nx = \"{{{{ op://v/i/f }}}}\"\n"),
        );
        p.fake_terraform("[ -n \"$TF_DATA_DIR\" ] && mkdir -p \"$TF_DATA_DIR\"\nexit 0");
        assert_eq!(tfp(&p, &["use", "dev"]).0, 0);
        std::fs::write(p.path("tmp/slots/dev/tfplan"), "").unwrap();
        std::fs::write(p.path("tmp/slots/dev/tfplan.meta.json"), "").unwrap();
        for args in [
            &["plan"][..],
            &["apply"],
            &["refresh"],
            &["destroy"],
            &["import", "a", "b"],
        ] {
            assert!(fails(&p, args).contains("`op inject` failed"), "{args:?}");
        }
    }

    #[test]
    fn a_failing_terraform_stops_the_command() {
        let (p, c) = bound("[ \"$2\" = init ] || [ \"$2\" = providers ] || exit 1\nexit 0");
        std::fs::create_dir_all(c.snapshots_dir("dev")).unwrap();
        std::fs::write(c.snapshots_dir("dev").join("s.tfstate"), "{}").unwrap();
        for args in [
            &["plan"][..],
            &["import", "a", "b"],
            &["state", "mv", "a", "b"],
            &["self", "snapshot", "create"],
            &["self", "snapshot", "create", "--dest", "x"],
            &["self", "snapshot", "restore", "-y"],
        ] {
            fails(&p, args);
        }
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        std::fs::write(c.tfplan_meta("dev"), "{}").unwrap();
        assert!(fails(&p, &["apply"]).contains("missing or non-int `schema_version`"));
        assert_eq!(tfp(&p, &["apply", "--force"]).0, 1);
        let quiet = Project::new("terraform_dir = \"t\"");
        quiet.fake_terraform("exit 2");
        assert_eq!(tfp(&quiet, &["fmt"]).0, 2);
    }

    #[test]
    fn state_pull_failures_explain_themselves() {
        let (p, _c) = bound("[ \"$3\" = pull ] && { echo denied >&2; exit 1; }\nexit 0");
        assert_eq!(
            fails(&p, &["state", "rm", "a"]),
            "terraform state pull failed: denied\n"
        );
        let (p, _c) = bound("[ \"$3\" = pull ] && exit 1\nexit 0");
        assert!(fails(&p, &["self", "snapshot", "create"]).starts_with("terraform state pull failed: exit status: 1"));
        std::fs::remove_file(p.path("bin/terraform")).unwrap();
        assert!(fails(&p, &["self", "snapshot", "create"]).contains("bin/terraform: "));
    }

    #[test]
    fn last_and_status_details() {
        let (p, c) = bound("");
        let last = crate::models::LastInvocation {
            timestamp: "t".into(),
            argv: strings(&["terraform"]),
            exit_code: None,
        };
        crate::models::Versioned::dump_file(&last, &c.last_invocation("dev")).unwrap();
        assert!(tfp(&p, &["last"]).1.contains("exit_code = (unknown)"));
        std::fs::write(c.last_invocation("dev"), "{}").unwrap();
        fails(&p, &["last"]);
        p.tfvars(
            "dev",
            r#"{"header":"terraform","project":"demo","env":{"B":"2","A":"1"}}"#,
        );
        assert_eq!(tfp(&p, &["use", "dev"]).0, 0);
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        let out = tfp(&p, &["status"]).1;
        assert!(out.contains("env keys    = A, B\nplan        = ready\n"), "{out}");
    }

    fn chmod(path: &std::path::Path, mode: u32) {
        std::fs::set_permissions(path, std::os::unix::fs::PermissionsExt::from_mode(mode)).unwrap();
    }

    #[test]
    fn plan_and_apply_hash_failures() {
        let (p, c) = bound("");
        std::fs::create_dir_all(c.tfplan_meta("dev")).unwrap();
        assert!(fails(&p, &["plan"]).contains("tfplan.meta.json"));
        std::fs::remove_dir(c.tfplan_meta("dev")).unwrap();
        let tf = p.path("terraform/demo/main.tf");
        chmod(&tf, 0o000);
        assert!(fails(&p, &["plan"]).contains("main.tf"));
        chmod(&tf, 0o644);
        assert_eq!(tfp(&p, &["plan"]).0, 0);
        std::fs::write(c.tfplan_file("dev"), "").unwrap();
        chmod(&tf, 0o000);
        assert!(fails(&p, &["apply"]).contains("main.tf"));
        chmod(&tf, 0o644);
        // The tfvars file is gone: the slot falls back to its saved state, whose tfvars cannot be read.
        std::fs::remove_file(p.path("tfvars/dev.tfvars")).unwrap();
        assert!(fails(&p, &["apply"]).contains("dev.tfvars"));
        assert!(fails(&p, &["plan"]).contains("dev.tfvars"));
    }

    #[test]
    fn doctor_checks_tmp_dir_is_git_ignored() {
        let p = Project::new("");
        let ignored = |p: &Project| {
            crate::self_commands::do_self_doctor(&p.config(), None)
                .into_iter()
                .find(|c| c.name == "tmp_dir git-ignored")
                .map(|c| c.ok)
        };
        assert_eq!(ignored(&p), None);
        assert!(
            std::process::Command::new("git")
                .args(["init", "-q"])
                .current_dir(&p.root)
                .status()
                .unwrap()
                .success()
        );
        assert_eq!(ignored(&p), Some(false));
        p.write(".gitignore", "tmp/\n");
        assert_eq!(ignored(&p), Some(true));
    }

    #[test]
    fn doctor_checks_each_secret_scheme_in_use() {
        let p = Project::new("");
        let doctor = |p: &Project, path: &std::path::Path| {
            let checks = crate::self_commands::do_self_doctor(&p.config(), Some(path.as_os_str()));
            checks
                .into_iter()
                .filter(|c| c.name.contains("secret"))
                .map(|c| (c.name, c.ok))
                .collect::<Vec<_>>()
        };
        let bin = p.path("bin");
        std::fs::create_dir_all(&bin).unwrap();
        assert_eq!(doctor(&p, &bin), [("secret references".to_owned(), true)]);
        p.tfvars("dev", r#"{"header":"terraform","project":"demo"}"#);
        p.write(
            "tfvars/dev.tfvars",
            "# {\"header\":\"terraform\",\"project\":\"demo\"}\na = \"{{ op://v/i/f }}\"\nb = \"{{ vault://k }}\"\n",
        );
        let checks = crate::self_commands::do_self_doctor(&p.config(), Some(bin.as_os_str()));
        let secret: Vec<(&str, bool)> = checks
            .iter()
            .filter(|c| c.name.contains("://"))
            .map(|c| (c.name.as_str(), c.ok))
            .collect();
        assert_eq!(
            secret,
            [
                ("op:// references: 1Password CLI `op`", false),
                ("vault:// references", false)
            ]
        );
        let op = p.write("bin/op", "#!/bin/sh\n");
        chmod(&op, 0o755);
        p.write(
            "tfvars/qa.tfvars",
            "# {\"header\":\"terraform\",\"project\":\"demo\"}\nk = \"{{ az-kv://kv/secrets/s }}\"\n",
        );
        let az_check = |p: &Project| {
            let checks = crate::self_commands::do_self_doctor(&p.config(), Some(bin.as_os_str()));
            checks
                .into_iter()
                .find(|c| c.name.starts_with("az-kv://"))
                .map(|c| c.ok)
        };
        assert_eq!(az_check(&p), Some(false));
        chmod(&p.write("bin/az", "#!/bin/sh\n"), 0o755);
        assert_eq!(az_check(&p), Some(true));
        let checks = crate::self_commands::do_self_doctor(&p.config(), Some(bin.as_os_str()));
        assert!(
            checks
                .iter()
                .any(|c| c.name.starts_with("op://") && c.ok && c.detail == op.display().to_string())
        );
    }

    #[test]
    fn use_failures_on_disk() {
        let (p, c) = bound("");
        std::fs::remove_file(p.path("tmp/active.lock")).unwrap();
        std::fs::create_dir_all(p.path("tmp/active.lock")).unwrap();
        assert!(fails(&p, &["use", "dev"]).contains("active.lock"));
        std::fs::remove_dir(p.path("tmp/active.lock")).unwrap();
        std::fs::remove_file(c.backend_hash("dev")).unwrap();
        std::fs::create_dir_all(c.backend_hash("dev")).unwrap();
        assert!(fails(&p, &["use", "dev"]).contains("backend.hash"));
        std::fs::remove_dir(c.backend_hash("dev")).unwrap();
        std::fs::remove_file(c.state_file("dev")).unwrap();
        std::fs::create_dir_all(c.state_file("dev").join("x")).unwrap();
        assert!(fails(&p, &["init"]).contains("state.json"));
        p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        std::fs::remove_dir_all(p.path("tmp/slots")).unwrap();
        std::fs::write(p.path("tmp/slots"), "").unwrap();
        assert!(fails(&p, &["use", "qa"]).contains("slots"));
    }

    #[test]
    fn plugin_cache_dir_disabled_or_broken() {
        for (setting, ok) in [("\"\"", true), ("\"tf_project.toml/cache\"", false)] {
            let p = Project::new(&format!("plugin_cache_dir = {setting}"));
            p.tfvars("dev", DEMO);
            p.fake_terraform("");
            assert_eq!(tfp(&p, &["use", "dev"]).0 == 0, ok, "{setting}");
        }
    }

    #[test]
    fn rm_and_refresh_failures() {
        let (p, c) = bound("");
        std::fs::create_dir_all(p.path("tmp/slots/old/sub")).unwrap();
        std::fs::write(p.path("tmp/slots/old/sub/f"), "").unwrap();
        chmod(&p.path("tmp/slots/old/sub"), 0o555);
        assert!(fails(&p, &["rm", "old"]).contains("old"));
        chmod(&p.path("tmp/slots/old/sub"), 0o755);
        let _held = crate::state::exclusive_lock(&c.apply_lock("dev")).unwrap();
        assert!(fails(&p, &["refresh"]).starts_with("Another tfp apply, refresh or destroy is already running"));
    }

    #[test]
    fn self_init_into_pyproject() {
        for (existing, expected) in [
            ("", "\n\n[tool.tf_project]"),
            ("[x]\n", "[x]\n\n[tool"),
            ("[x]\n\n", "[x]\n\n[tool"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let pyproject = dir.path().join("pyproject.toml");
            std::fs::write(&pyproject, existing).unwrap();
            let (code, out, _) = tfp_in(dir.path(), &["self", "init"], "");
            assert_eq!(
                (code, out),
                (0, format!("Wrote tf_project config to {}\n", pyproject.display()))
            );
            let text = std::fs::read_to_string(&pyproject).unwrap();
            assert!(
                text.starts_with(&format!("{existing}{}", &expected[existing.len()..])),
                "{text:?}"
            );
            assert!(crate::config::Config::discover(dir.path()).is_ok());
            assert!(
                tfp_in(dir.path(), &["self", "init"], "")
                    .2
                    .starts_with("[tool.tf_project] already exists in ")
            );
        }
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("pyproject.toml"), "not toml [").unwrap();
        assert_eq!(tfp_in(dir.path(), &["self", "init"], "").0, 1);
        assert_eq!(tfp_in(dir.path(), &["self", "config", "path"], "").0, 1);
        chmod(dir.path(), 0o555);
        std::fs::remove_file(dir.path().join("pyproject.toml")).unwrap_or(());
        assert_eq!(tfp_in(dir.path(), &["self", "init"], "").0, 1);
        chmod(dir.path(), 0o755);
        std::fs::remove_file(dir.path().join("pyproject.toml")).unwrap();
        chmod(dir.path(), 0o555);
        assert_eq!(tfp_in(dir.path(), &["self", "init"], "").0, 1);
        chmod(dir.path(), 0o755);
    }

    #[test]
    fn self_init_cannot_read_or_write_pyproject() {
        let dir = tempfile::tempdir().unwrap();
        let pyproject = dir.path().join("pyproject.toml");
        std::fs::write(&pyproject, "").unwrap();
        for mode in [0o000, 0o444] {
            chmod(&pyproject, mode);
            assert!(
                tfp_in(dir.path(), &["self", "init"], "").2.contains("pyproject.toml"),
                "{mode:o}"
            );
        }
    }

    #[test]
    fn self_state_clear_failure() {
        let (p, c) = bound("");
        std::fs::remove_file(c.state_file("dev")).unwrap();
        std::fs::create_dir_all(c.state_file("dev").join("x")).unwrap();
        fails(&p, &["self", "state", "clear", "--all"]);
    }

    #[test]
    fn doctor_reports_problems() {
        let p = Project::new("terraform_dir = \"missing\"\n[tf_project.programs]\nop = \"no-such-op\"");
        p.write("tfvars/a.tfvars", &format!("# {DEMO}\nx = \"{{{{ op://v/i/f }}}}\"\n"));
        p.tfvars("b", r#"{"header":"terraform","project":"demo","slot":"a"}"#);
        for d in ["tmp/slots/x", "tmp/slots/y"] {
            std::fs::create_dir_all(p.path(d)).unwrap();
        }
        let (code, out, _) = tfp(&p, &["self", "doctor"]);
        assert_eq!(code, 1);
        for line in [
            "[FAIL] terraform binary",
            "[FAIL] op:// references: 1Password CLI `no-such-op`",
            "[FAIL] terraform_dir exists",
            "[FAIL] slot uniqueness",
            "no banner defines x, y",
        ] {
            assert!(out.contains(line), "{line}\n{out}");
        }
        std::fs::write(
            p.path("tf_project.toml"),
            "[tf_project]\nterraform_binary = \"sh\"\n[tf_project.programs]\nop = \"sh\"\n",
        )
        .unwrap();
        let out = tfp(&p, &["self", "doctor"]).1;
        assert!(
            out.contains("[OK  ] terraform binary `sh`") && out.contains("[OK  ] op:// references: 1Password CLI `sh`"),
            "{out}"
        );
        assert!(fails(&p, &["self", "banner", "check", "tfvars/none.tfvars"]).contains("none.tfvars"));
    }

    #[test]
    fn tfvars_without_a_slot_and_shared_states_are_reported() {
        let (p, _c) = bound("");
        let shared = r#"{"header":"terraform","project":"demo","state_key":"/demo/dev.tfstate","slot":"dev2"}"#;
        p.tfvars("dev2", shared);
        let (code, out, _) = tfp(&p, &["self", "doctor"]);
        assert_eq!(code, 0, "{out}");
        assert!(
            out.contains("; \"dev\" and \"dev2\" share state \"demo/dev.tfstate\" (state_key)"),
            "{out}"
        );
        let line = |out: &str| {
            let line = out.lines().find(|l| l.contains("tfvars without a banner")).unwrap();
            line.split_whitespace().collect::<Vec<_>>().join(" ")
        };
        assert_eq!(line(&out), "[OK ] tfvars without a banner none");
        p.write("tfvars/none.tfvars", "x = 1\n");
        let (code, out, _) = tfp(&p, &["self", "doctor"]);
        assert_eq!(code, 1);
        assert!(line(&out).starts_with("[FAIL] tfvars without a banner /"), "{out}");
        assert!(line(&out).ends_with("none.tfvars (no banner)"), "{out}");
        let warning = format!(
            "warning: no slot from {} (no banner)\n",
            p.path("tfvars/none.tfvars").display()
        );
        assert!(tfp(&p, &["use", "dev"]).2.starts_with(&warning));
        assert!(tfp(&p, &["init"]).2.starts_with(&warning));
        let (_, out, err) = tfp(&p, &["ls", "--json"]);
        assert_eq!(err, warning);
        let json: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(json["skipped"][0]["reason"], "no banner");
        // A default key shared by accident is refused.
        p.tfvars("dev2", r#"{"header":"terraform","project":"demo","slot":"dev2"}"#);
        p.tfvars("dev", r#"{"header":"terraform","project":"demo","slot":"dev"}"#);
        std::fs::write(
            p.path("tf_project.toml"),
            "[tf_project]\nterraform_binary = \"bin/terraform\"\nbackend_config = { key = \"one\" }\n",
        )
        .unwrap();
        let (code, _, err) = tfp(&p, &["use", "dev"]);
        assert!(code == 1 && err.contains("share one remote state"), "{err}");
        assert!(
            tfp(&p, &["ls"])
                .2
                .contains("warning: slots \"dev\" and \"dev2\" share one remote state")
        );
    }

    #[test]
    fn trace_every_command() {
        let (p, _c) = bound("");
        for (command, expected) in [
            ("use", " init -upgrade -reconfigure "),
            ("init", " init -upgrade -reconfigure "),
            ("apply", " apply -var-file=<DECRYPTED_TFVARS> "),
            ("refresh", " apply -var-file=<DECRYPTED_TFVARS>"),
            ("destroy", " destroy -var-file=<DECRYPTED_TFVARS>"),
            ("output", " output -json"),
            (
                "import",
                " import -var-file=<DECRYPTED_TFVARS> <RESOURCE_ADDRESS> <RESOURCE_ID>",
            ),
        ] {
            let out = tfp(&p, &["self", "trace", command]).1;
            assert!(
                shlex::split(&out).unwrap().join(" ").contains(expected),
                "{command}: {out}"
            );
        }
    }

    #[test]
    fn snapshot_lifecycle() {
        let (p, c) = bound("[ \"$3\" = pull ] && echo '{}'\nexit 0");
        let dir = c.snapshots_dir("dev");
        // The default retention keeps both; a pre-existing directory named like a snapshot is ignored.
        std::fs::create_dir_all(dir.join("dir.tfstate")).unwrap();
        assert_eq!(tfp(&p, &["state", "mv", "a", "b"]).0, 0);
        assert_eq!(tfp(&p, &["self", "snapshot", "create"]).0, 0);
        let listed = tfp(&p, &["self", "snapshot", "list"]).1;
        assert_eq!(listed.lines().count(), 2, "{listed}");
        assert!(
            listed.lines().last().unwrap().contains("-pre-state-mv.tfstate"),
            "{listed}"
        );
        // Two snapshots with the same time sort by name, newest name first.
        let same = std::time::SystemTime::UNIX_EPOCH + std::time::Duration::from_secs(1_000_000_000);
        for name in ["a.tfstate", "b.tfstate"] {
            std::fs::write(dir.join(name), "{}").unwrap();
            std::fs::File::options()
                .write(true)
                .open(dir.join(name))
                .unwrap()
                .set_modified(same)
                .unwrap();
        }
        let listed = tfp(&p, &["self", "snapshot", "list"]).1;
        assert!(listed.lines().nth(2).unwrap().starts_with("b.tfstate\t"), "{listed}");
        // Restore by stem or file name; an unknown name lists what there is.
        assert!(
            tfp(&p, &["self", "snapshot", "restore", "-y", "a"])
                .1
                .trim_end()
                .ends_with("a.tfstate")
        );
        assert!(
            tfp_in(&p.root, &["self", "snapshot", "restore", "b.tfstate"], "y\n")
                .1
                .trim_end()
                .ends_with("b.tfstate")
        );
        assert!(
            fails(&p, &["self", "snapshot", "restore", "-y", "zzz"])
                .starts_with("No snapshot named \"zzz\" for slot \"dev\". Available:\n  ")
        );
        // Retention 1 prunes everything but the newest.
        std::fs::write(
            p.path("tf_project.toml"),
            "[tf_project]\nterraform_binary = \"bin/terraform\"\nsnapshot_retention = 1\n",
        )
        .unwrap();
        assert_eq!(tfp(&p, &["self", "snapshot", "create"]).0, 0);
        assert_eq!(tfp(&p, &["self", "snapshot", "list"]).1.lines().count(), 1);
        // Retention 0 takes no automatic snapshots.
        std::fs::write(
            p.path("tf_project.toml"),
            "[tf_project]\nterraform_binary = \"bin/terraform\"\nsnapshot_retention = 0\n",
        )
        .unwrap();
        assert_eq!(tfp(&p, &["state", "rm", "a"]).0, 0);
        assert_eq!(tfp(&p, &["self", "snapshot", "list"]).1.lines().count(), 1);
    }

    #[test]
    fn snapshot_write_failures() {
        let (p, c) = bound("[ \"$3\" = pull ] && echo '{}'\nexit 0");
        assert!(fails(&p, &["self", "snapshot", "create", "--dest", "tf_project.toml/x"]).contains("tf_project.toml"));
        std::fs::create_dir_all(p.path("out")).unwrap();
        assert!(fails(&p, &["self", "snapshot", "create", "--dest", "out"]).contains("out"));
        std::fs::create_dir_all(c.snapshots_dir("dev")).unwrap();
        chmod(&c.snapshots_dir("dev"), 0o555);
        assert!(fails(&p, &["self", "snapshot", "create"]).contains("snapshots"));
        chmod(&c.snapshots_dir("dev"), 0o755);
        std::fs::remove_dir(c.snapshots_dir("dev")).unwrap();
        std::fs::write(c.snapshots_dir("dev"), "").unwrap();
        assert!(fails(&p, &["self", "snapshot", "create"]).contains("snapshots"));
    }

    #[test]
    fn lock_break_without_backend_metadata() {
        let p = Project::new("");
        p.tfvars("dev", r#"{"header":"terraform","project":"demo","backend_config":{"storage_account_name":"a","container_name":"c"},"state_key":"/k"}"#);
        p.fake_terraform("[ -n \"$TF_DATA_DIR\" ] && mkdir -p \"$TF_DATA_DIR\"\nexit 0");
        assert_eq!(tfp(&p, &["use", "dev"]).0, 0);
        std::fs::write(p.path("tfvars/dev.tfvars"), "# {\"header\":\"terraform\",\"project\":\"demo\",\"backend_config\":{\"storage_account_name\":\"a\",\"container_name\":\"\"}}\n").unwrap();
        assert_eq!(tfp(&p, &["use", "dev"]).0, 0);
        assert!(fails(&p, &["self", "lock", "break", "-y"]).starts_with("Azure backend metadata missing"));
    }

    /// A project whose slot `dev` has the backend `backend` and the environment `env`, with fake `az` and `aws`
    /// in `bin/` printing `cloud_out`.
    fn cloud(backend: &str, env: &str, cloud_out: &str) -> Project {
        let p = Project::new("");
        p.tfvars(
            "dev",
            &format!(r#"{{"header":"terraform","project":"demo","backend_config":{backend},"env":{env}}}"#),
        );
        p.fake_terraform("[ -n \"$TF_DATA_DIR\" ] && mkdir -p \"$TF_DATA_DIR\"\n[ \"$2\" = force-unlock ] && exit \"${UNLOCK_CODE:-0}\"\nexit 0");
        p.write("bin/cloud.out", cloud_out);
        for cli in ["az", "aws"] {
            let path = p.write(
                &format!("bin/{cli}"),
                "#!/bin/sh\necho \"$@\" >> \"$0.log\"\ncat \"$(dirname \"$0\")/cloud.out\"\n",
            );
            chmod(&path, 0o755);
        }
        assert_eq!(tfp(&p, &["use", "dev"]).0, 0);
        p
    }

    /// `tfp <args>` with the project's `bin/` first on the `PATH` it starts the cloud CLIs with.
    fn tfp_cloud(p: &Project, args: &[&str], input: &str) -> (i32, String, String) {
        let mut s = Streams::with_input(input);
        let mut ctx = s.ctx(&p.root);
        let path = format!(
            "{}:{}",
            p.path("bin").display(),
            std::env::var("PATH").unwrap_or_default()
        );
        ctx.search_path = Some(path.into());
        let code = run(
            [&["tfp"][..], args].concat().iter().map(|a| (*a).to_owned()).collect(),
            &mut ctx,
        );
        (code, s.out(), s.err())
    }

    const AZURE: &str = r#"{"storage_account_name":"acct","container_name":"c","key":"k"}"#;
    const AZURE_LOCKED: &str = r#"{"properties":{"lease":{"status":"locked","state":"leased"}},"metadata":{"terraformlockid":"eyJJRCI6ImFiYyIsIldobyI6Im1lIn0="}}"#;

    #[test]
    fn azure_lock_status_and_polite_break() {
        let p = cloud(AZURE, "{}", AZURE_LOCKED);
        let (code, out, err) = tfp_cloud(&p, &["self", "lock", "status"], "");
        assert_eq!(code, 2, "{err}");
        assert!(
            out.contains("locked         = true\n") && out.contains("lock_id        = abc\n"),
            "{out}"
        );
        assert!(
            out.contains("lock_operation = (unknown)\n") && out.ends_with("tfp tf force-unlock abc\n"),
            "{out}"
        );
        let (code, out, _) = tfp_cloud(&p, &["self", "lock", "break"], "y\n");
        assert_eq!(
            (code, out.as_str()),
            (0, "Released via terraform force-unlock (lock_id=abc).\n")
        );
        assert!(p.terraform_calls().last().unwrap().ends_with("force-unlock -force abc"));
    }

    #[test]
    fn azure_lock_breaks_the_lease_when_terraform_cannot() {
        let p = cloud(AZURE, r#"{"UNLOCK_CODE":"1"}"#, AZURE_LOCKED);
        assert_eq!(
            tfp_cloud(&p, &["self", "lock", "break", "-y"], "").1,
            "Broke the azurerm lock at the backend level.\n"
        );
        assert_eq!(
            tfp_cloud(&p, &["self", "lock", "break", "-y", "--blunt"], "").1,
            "Broke the azurerm lock at the backend level.\n"
        );
        let calls = std::fs::read_to_string(p.path("bin/az.log")).unwrap();
        assert!(
            calls.ends_with("storage blob lease break --account-name acct --container-name c --blob-name k\n"),
            "{calls}"
        );
    }

    #[test]
    fn s3_lock_with_nothing_to_break() {
        let p = cloud(r#"{"dynamodb_table":"t","bucket":"b","key":"k"}"#, "{}", "{}");
        let (code, out, _) = tfp_cloud(&p, &["self", "lock", "status"], "");
        assert_eq!(
            (code, out.as_str()),
            (
                0,
                "backend        = s3\nlocked         = false\ndetail         = no_lock\n"
            )
        );
        let (code, _, err) = tfp_cloud(&p, &["self", "lock", "break", "-y"], "");
        assert!(
            code == 1 && err.starts_with("No lock to break on the s3 backend."),
            "{err}"
        );
    }

    #[test]
    fn lock_on_an_unsupported_backend() {
        let p = cloud("{}", "{}", "{}");
        for args in [&["self", "lock", "status"][..], &["self", "lock", "break", "-y"]] {
            let (code, _, err) = tfp_cloud(&p, args, "");
            assert!(
                code == 1 && err.starts_with("`tfp self lock` handles azurerm and s3"),
                "{err}"
            );
        }
    }

    /// Slot `dev` bound with `env` in its banner, a plan saved, and a fake `az` running `az_body`.
    fn azure(env: &str, az_body: &str) -> Project {
        let (p, _c) = bound("[ \"$2 $3\" = \"state pull\" ] && echo '{}'\nexit 0");
        p.tfvars(
            "dev",
            &format!(r#"{{"header":"terraform","project":"demo","state_key":"/demo/dev.tfstate","env":{env}}}"#),
        );
        chmod(&p.write("bin/az", &format!("#!/bin/sh\n{az_body}\n")), 0o755);
        assert_eq!(tfp(&p, &["plan"]).0, 0);
        std::fs::write(p.path("tmp/slots/dev/tfplan"), "").unwrap();
        p
    }

    const ACCOUNT: &str =
        r#"echo '{"id":"sub-1","name":"Default","tenantId":"t-1","user":{"name":"me@x","type":"user"}}'"#;

    #[test]
    fn azure_identity_is_checked_before_changes() {
        let p = azure(r#"{"ARM_TENANT_ID":"t-2"}"#, ACCOUNT);
        assert_eq!(tfp(&p, &["self", "snapshot", "create"]).0, 0);
        let calls = p.terraform_calls().len();
        for args in [
            &["apply"][..],
            &["destroy"],
            &["import", "a.b", "id"],
            &["state", "mv", "a", "b"],
            &["self", "snapshot", "restore", "-y"],
        ] {
            let (code, _, err) = tfp_cloud(&p, args, "");
            assert_eq!(code, 1, "{args:?}");
            assert!(
                err.ends_with(
                    "slot \"dev\" expects tenant t-2 but az is signed in to tenant t-1 (me@x) - \
                     run `az login --tenant t-2`\n"
                ),
                "{args:?}: {err}"
            );
        }
        assert_eq!(p.terraform_calls().len(), calls, "terraform never ran");
        // Reading, and --dry-run, don't ask az.
        assert_eq!(tfp_cloud(&p, &["state", "list"], "").0, 0);
        assert_eq!(tfp_cloud(&p, &["--dry-run", "destroy"], "").0, 0);
        assert!(!p.path("bin/az.log").exists());
        let (code, out, _) = tfp_cloud(&p, &["self", "doctor"], "");
        assert_eq!(code, 1);
        assert!(
            out.contains("me@x in tenant t-1; slot \"dev\" expects tenant t-2"),
            "{out}"
        );
    }

    #[test]
    fn azure_identity_warns_without_a_subscription() {
        let p = azure(r#"{"ARM_TENANT_ID":"t-1"}"#, ACCOUNT);
        let (code, _, err) = tfp_cloud(&p, &["apply"], "");
        assert_eq!(code, 0, "{err}");
        assert_eq!(
            err,
            "warning: slot \"dev\" sets no ARM_SUBSCRIPTION_ID; Terraform will use az's default subscription \
             Default (sub-1)\n"
        );
        let (code, out, _) = tfp_cloud(&p, &["self", "doctor"], "");
        assert_eq!(code, 0, "{out}");
        assert!(
            out.contains("me@x in tenant t-1; slot \"dev\" sets no ARM_SUBSCRIPTION_ID"),
            "{out}"
        );
    }

    #[test]
    fn azure_identity_that_matches_is_quiet() {
        let p = azure(r#"{"ARM_TENANT_ID":"t-1","ARM_SUBSCRIPTION_ID":"s"}"#, ACCOUNT);
        assert_eq!(tfp_cloud(&p, &["destroy"], ""), (0, String::new(), String::new()));
        // A configured az that isn't there is not looked for on PATH.
        let toml = std::fs::read_to_string(p.path("tf_project.toml")).unwrap();
        std::fs::write(
            p.path("tf_project.toml"),
            format!("{toml}[tf_project.programs]\naz = \"/no/such/az\"\n"),
        )
        .unwrap();
        assert_eq!(tfp(&p, &["destroy"]).0, 0);
    }

    #[test]
    fn azure_identity_when_az_is_missing_or_signed_out() {
        // Signed out, or printing something else.
        for body in ["echo 'Please run az login' >&2; exit 1", "echo nope"] {
            let p = azure(r#"{"ARM_SUBSCRIPTION_ID":"s"}"#, body);
            let (code, _, err) = tfp_cloud(&p, &["destroy"], "");
            assert!(
                code == 1 && err.contains("`az account show` failed (") && err.ends_with("Run `az login`.\n"),
                "{err}"
            );
            let (code, out, _) = tfp_cloud(&p, &["self", "doctor"], "");
            assert!(code == 1 && out.contains("Run `az login`."), "{out}");
        }
        // Found but cannot start.
        let p = azure(r#"{"ARM_SUBSCRIPTION_ID":"s"}"#, "");
        p.write("bin/az", "#!/no/such/shell\n");
        let (code, _, err) = tfp_cloud(&p, &["destroy"], "");
        assert!(code == 1 && err.contains("bin/az: "), "{err}");
        // Not installed: commands go ahead, doctor says so.
        std::fs::remove_file(p.path("bin/az")).unwrap();
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.search_path = Some(p.path("bin").into());
        assert_eq!(run(strings(&["tfp", "destroy"]), &mut ctx), 0, "{}", s.err());
        let checks = crate::self_commands::do_self_doctor(&p.config(), Some(p.path("bin").as_os_str()));
        let az = checks.iter().find(|c| c.name == "azure identity").unwrap();
        assert!(!az.ok && az.detail.starts_with("`az` not found"), "{}", az.detail);
    }

    #[test]
    fn drift_summary_and_exit_codes() {
        let (p, c) =
            bound("[ \"$2\" = plan ] && case \"$TF_DATA_DIR\" in */qa/*) exit 2;; */bad/*) exit 1;; esac\nexit 0");
        let (code, out, err) = tfp(&p, &["drift"]);
        assert_eq!((code, out.as_str()), (0, "SLOT  RESULT\ndev   clean\n"), "{err}");
        assert_eq!(err, "==== drift: slot \"dev\" ====\n");
        // Drift alone exits 2; uninitialised slots are skipped and don't count.
        p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        p.tfvars("bad", r#"{"header":"terraform","project":"demo"}"#);
        assert_eq!(tfp(&p, &["use", "qa"]).0, 0);
        let (code, out, err) = tfp(&p, &["drift"]);
        assert_eq!(code, 2);
        assert_eq!(
            out,
            "SLOT  RESULT\nbad   skipped: not initialised - run `tfp use bad`\ndev   clean\nqa    drift\n"
        );
        assert!(
            err.starts_with("slot \"bad\": skipped: not initialised - run `tfp use bad`\n"),
            "{err}"
        );
        // Any error exits 1.
        assert_eq!(tfp(&p, &["use", "bad"]).0, 0);
        let (code, out, _) = tfp(&p, &["drift", "--json"]);
        assert_eq!(code, 1);
        let json: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(
            json,
            serde_json::json!([
                {"slot": "bad", "result": "error", "reason": "exit code 1"},
                {"slot": "dev", "result": "clean"},
                {"slot": "qa", "result": "drift"},
            ])
        );
        // --dry-run runs nothing.
        let (code, _, err) = tfp(&p, &["--dry-run", "drift"]);
        assert!(
            code == 0 && err.contains("[dry-run] ") && err.contains(" -detailed-exitcode '-lock=false' '-input=false'"),
            "{err}"
        );
        assert_eq!(crate::state::read_active(&c).unwrap().as_deref(), Some("bad"));
    }
}
