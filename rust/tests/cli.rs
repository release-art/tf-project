//! The built programs, run as a user would: both names, same output.

use std::process::Command;

use pretty_assertions::assert_eq;

#[test]
fn both_names_print_the_version() {
    for exe in [env!("CARGO_BIN_EXE_tf-project"), env!("CARGO_BIN_EXE_tfp")] {
        let out = Command::new(exe).arg("--version").output().unwrap();
        assert!(out.status.success());
        assert_eq!(
            String::from_utf8(out.stdout).unwrap(),
            format!("{}\n", env!("CARGO_PKG_VERSION"))
        );
    }
}

#[test]
fn unknown_flag_fails_with_usage_code() {
    let out = Command::new(env!("CARGO_BIN_EXE_tfp")).arg("--nope").output().unwrap();
    assert_eq!(out.status.code(), Some(2));
}
