//! Running Terraform: the slot's environment, `--dry-run` / `--verbose`, and the `tfp last` record.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};

use crate::config::Config;
use crate::models::{LastInvocation, MyState, Versioned};
use crate::text::{shell_join, utc};
use crate::{Ctx, Error, Result, say};

/// Environment variables set on top of tfp's own for a Terraform run.
pub type Env = BTreeMap<String, String>;

/// How Terraform is run, from the global flags.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Options {
    /// Print the command line and run nothing.
    pub dry_run: bool,
    /// Print the command line before running it.
    pub verbose: bool,
    /// Where to record the invocation for `tfp last`; nowhere when no slot is active.
    pub last_invocation: Option<PathBuf>,
}

/// Run `cmd` with `env` on top of tfp's environment and wait for it. Ctrl-C reaches Terraform, which stops cleanly;
/// tfp waits for it rather than dying first.
///
/// # Errors
/// [`Error::Exit`] with Terraform's exit code when it is not 0 (127 when the program is not found).
pub fn run(ctx: &mut Ctx, cmd: &[String], env: &Env) -> Result<()> {
    let [program, args @ ..] = cmd else {
        return Err(Error::Msg("empty command".into()));
    };
    if ctx.terraform.verbose || ctx.terraform.dry_run {
        let prefix = if ctx.terraform.dry_run { "[dry-run]" } else { "$" };
        say!(ctx.err, "{prefix} {}", shell_join(cmd));
    }
    if ctx.terraform.dry_run {
        return Ok(());
    }
    let code = match Command::new(program).args(args).envs(env).status() {
        Ok(status) => exit_code(status),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            say!(ctx.err, "{program}: command not found");
            127
        }
        Err(e) => return Err(Error::Msg(format!("{program}: {e}"))),
    };
    if let Some(path) = &ctx.terraform.last_invocation {
        record(path, cmd, code);
    }
    if code == 0 { Ok(()) } else { Err(Error::Exit(code)) }
}

/// The exit code a shell would report: the process's own, or 128 + the signal that killed it.
fn exit_code(status: ExitStatus) -> i32 {
    #[cfg(unix)]
    if let Some(signal) = std::os::unix::process::ExitStatusExt::signal(&status) {
        return 128 + signal;
    }
    status.code().unwrap_or(1)
}

/// Record the invocation for `tfp last`; a failure to record does not fail the command.
fn record(path: &Path, cmd: &[String], exit_code: i32) {
    let last = LastInvocation {
        timestamp: utc(std::time::SystemTime::now(), false),
        argv: cmd.to_vec(),
        exit_code: Some(exit_code.into()),
    };
    let _ = last.dump_file(path);
}

/// The environment for Terraform running against a slot's saved state.
#[must_use]
pub fn env_for_slot(config: &Config, state: &MyState) -> Env {
    merged_env(
        &state.environ,
        Some(&config.data_dir(&state.slot)),
        config.plugin_cache_dir.as_deref(),
    )
}

/// `extra`, then `TF_DATA_DIR` and the plugin cache variables when given.
#[must_use]
pub fn merged_env(extra: &Env, data_dir: Option<&Path>, plugin_cache_dir: Option<&Path>) -> Env {
    let mut env = extra.clone();
    if let Some(dir) = data_dir {
        env.insert("TF_DATA_DIR".into(), dir.display().to_string());
    }
    if let Some(dir) = plugin_cache_dir {
        env.insert("TF_PLUGIN_CACHE_DIR".into(), dir.display().to_string());
        env.insert("TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE".into(), "1".into());
    }
    env
}

/// `-target=<t>` for each target.
#[must_use]
pub fn target_args(targets: &[String]) -> Vec<String> {
    targets.iter().map(|t| format!("-target={t}")).collect()
}

/// `-replace=<r>` for each address.
#[must_use]
pub fn replace_args(replaces: &[String]) -> Vec<String> {
    replaces.iter().map(|r| format!("-replace={r}")).collect()
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use pretty_assertions::assert_eq;

    use super::{Env, merged_env, replace_args, run, target_args};
    use crate::Error;
    use crate::models::{LastInvocation, Versioned};
    use crate::testing::{Project, Streams};

    fn argv(args: &[&str]) -> Vec<String> {
        args.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn runs_records_and_reports_exit_codes() {
        let p = Project::new("");
        let tf =
            p.fake_terraform("echo \"TF_DATA_DIR=$TF_DATA_DIR\" >> \"$(dirname \"$0\")/env.log\"\nexit ${CODE:-0}");
        let tf = tf.display().to_string();
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.terraform.verbose = true;
        let last = p.path("tmp/last.json");
        ctx.terraform.last_invocation = Some(last.clone());
        let env = Env::from([("TF_DATA_DIR".into(), "/d".into())]);
        run(&mut ctx, &argv(&[&tf, "plan", "a b"]), &env).unwrap();
        let fail = Env::from([("CODE".into(), "3".into())]);
        assert_eq!(run(&mut ctx, &argv(&[&tf, "apply"]), &fail), Err(Error::Exit(3)));
        assert_eq!(p.terraform_calls(), ["plan a b", "apply"]);
        assert_eq!(
            std::fs::read_to_string(p.path("bin/env.log")).unwrap(),
            "TF_DATA_DIR=/d\nTF_DATA_DIR=\n"
        );
        let recorded = LastInvocation::load_file(&last).unwrap();
        assert_eq!((recorded.argv, recorded.exit_code), (argv(&[&tf, "apply"]), Some(3)));
        assert_eq!(
            s.err(),
            format!("$ {tf} plan {}\n$ {tf} apply\n", shlex::try_quote("a b").unwrap())
        );
    }

    #[test]
    fn a_signal_reads_as_128_plus_it() {
        let p = Project::new("");
        let tf = p.fake_terraform("kill -9 $$").display().to_string();
        let mut s = Streams::default();
        assert_eq!(run(&mut s.ctx(&p.root), &[tf], &Env::new()), Err(Error::Exit(137)));
    }

    #[test]
    fn dry_run_runs_nothing() {
        let mut s = Streams::default();
        let mut ctx = s.ctx(Path::new("/"));
        ctx.terraform.dry_run = true;
        run(&mut ctx, &argv(&["/no/such/terraform", "plan"]), &Env::new()).unwrap();
        assert_eq!(s.err(), "[dry-run] /no/such/terraform plan\n");
    }

    #[test]
    fn missing_and_unrunnable_programs() {
        let p = Project::new("");
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.terraform.last_invocation = Some(p.path("tmp/last.json"));
        assert_eq!(
            run(&mut ctx, &argv(&["/no/such/terraform"]), &Env::new()),
            Err(Error::Exit(127))
        );
        assert!(matches!(
            run(&mut ctx, &argv(&[p.root.to_str().unwrap()]), &Env::new()),
            Err(Error::Msg(_))
        ));
        assert!(matches!(run(&mut ctx, &[], &Env::new()), Err(Error::Msg(_))));
        assert_eq!(s.err(), "/no/such/terraform: command not found\n");
        assert_eq!(
            LastInvocation::load_file(&p.path("tmp/last.json")).unwrap().exit_code,
            Some(127)
        );
    }

    #[test]
    fn env_layers() {
        let extra = Env::from([("A".into(), "1".into())]);
        let env = merged_env(&extra, Some(Path::new("/d")), Some(Path::new("/c")));
        assert_eq!(
            env.keys().collect::<Vec<_>>(),
            [
                "A",
                "TF_DATA_DIR",
                "TF_PLUGIN_CACHE_DIR",
                "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"
            ]
        );
        assert_eq!(merged_env(&extra, None, None), extra);
        assert_eq!(target_args(&argv(&["a"])), ["-target=a"]);
        assert_eq!(replace_args(&argv(&["b"])), ["-replace=b"]);
    }
}
