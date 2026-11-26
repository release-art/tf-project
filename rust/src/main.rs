//! The `tf-project` program. See the library for everything it does.

use std::process::ExitCode;

fn main() -> ExitCode {
    tf_project::run(
        std::env::args_os().collect(),
        &mut std::io::stdout(),
        &mut std::io::stderr(),
    )
    .unwrap_or(ExitCode::FAILURE)
}
