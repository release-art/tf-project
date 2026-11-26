//! tf-project: a Terraform / OpenTofu wrapper with named per-tfvars slots.
//!
//! The library is the whole program; `src/main.rs` and `src/bin/tfp.rs` are its two names.

use std::ffi::OsString;
use std::io::Write;
use std::process::ExitCode;

use clap::Parser;

/// Terraform / OpenTofu wrapper with named per-tfvars slots.
#[derive(Debug, Parser)]
#[command(name = "tf-project", disable_version_flag = true)]
struct Cli {
    /// Show version and exit.
    #[arg(long)]
    version: bool,
}

/// Run the command line given by `args` (the program name first), writing output to `out` and errors to `err`.
///
/// # Errors
/// Fails only when `out` or `err` cannot be written.
pub fn run(args: Vec<OsString>, out: &mut dyn Write, err: &mut dyn Write) -> std::io::Result<ExitCode> {
    let cli = match Cli::try_parse_from(args) {
        Ok(cli) => cli,
        Err(e) => {
            // --help lands here too, with exit code 0 and for stdout; clap knows which.
            let stream: &mut dyn Write = if e.use_stderr() { err } else { out };
            write!(stream, "{}", e.render())?;
            return Ok(ExitCode::from(u8::try_from(e.exit_code()).unwrap_or(2)));
        }
    };
    if cli.version {
        // Bare version, as the Python CLI printed it.
        writeln!(out, "{}", env!("CARGO_PKG_VERSION"))?;
    }
    Ok(ExitCode::SUCCESS)
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::run;

    /// A stream with no room left: every write fails, as on a closed pipe.
    fn full() -> &'static mut [u8] {
        &mut []
    }

    fn argv(args: &[&str]) -> Vec<std::ffi::OsString> {
        args.iter().map(Into::into).collect()
    }

    fn output(args: &[&str]) -> String {
        let mut out = Vec::new();
        let _code = run(argv(args), &mut out, &mut full()).unwrap();
        String::from_utf8(out).unwrap()
    }

    #[test]
    fn version_is_bare() {
        assert_eq!(
            output(&["tf-project", "--version"]),
            format!("{}\n", env!("CARGO_PKG_VERSION"))
        );
    }

    #[test]
    fn help_mentions_terraform() {
        assert!(output(&["tf-project", "--help"]).contains("Terraform"));
    }

    #[test]
    fn a_closed_stream_is_an_error() {
        assert!(run(argv(&["tf-project", "--version"]), &mut full(), &mut full()).is_err());
        assert!(run(argv(&["tf-project", "--nope"]), &mut full(), &mut full()).is_err());
    }

    #[test]
    fn no_args_prints_nothing() {
        assert_eq!(output(&["tf-project"]), "");
    }
}
