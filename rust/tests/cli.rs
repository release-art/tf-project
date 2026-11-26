//! The built programs, run as a user would: both names, usage errors, and Ctrl-C.
#![cfg(unix)]

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use pretty_assertions::assert_eq;

/// A project bound to slot `dev` through `tfp use`, with a fake terraform under `bin/`.
struct Project {
    _dir: tempfile::TempDir,
    root: PathBuf,
}

fn script(path: &Path, body: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

impl Project {
    fn new(backend: &str) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        std::fs::create_dir_all(root.join("terraform/demo")).unwrap();
        std::fs::create_dir_all(root.join("tfvars")).unwrap();
        std::fs::write(
            root.join("tf_project.toml"),
            "[tf_project]\nterraform_binary = \"bin/terraform\"\n",
        )
        .unwrap();
        std::fs::write(
            root.join("tfvars/dev.tfvars"),
            format!("# {{\"header\":\"terraform\",\"project\":\"demo\",\"backend_config\":{backend}}}\n"),
        )
        .unwrap();
        script(
            &root.join("bin/terraform"),
            "echo \"$@\" >> \"$(dirname \"$0\")/terraform.log\"\n\
             [ -n \"$TF_DATA_DIR\" ] && mkdir -p \"$TF_DATA_DIR\"\n\
             [ \"$2\" = sleep ] && { sleep 1; exit 7; }\n\
             exit 0",
        );
        let project = Self { _dir: dir, root };
        assert!(project.tfp(&["use", "dev"]).status.success());
        project
    }

    fn command(&self, program: &str, args: &[&str]) -> Command {
        let mut cmd = Command::new(program);
        let path = format!(
            "{}:{}",
            self.root.join("bin").display(),
            std::env::var("PATH").unwrap_or_default()
        );
        cmd.args(args)
            .current_dir(&self.root)
            .env("PATH", path)
            .env_remove("TFP_SLOT");
        cmd
    }

    fn tfp(&self, args: &[&str]) -> Output {
        self.command(env!("CARGO_BIN_EXE_tfp"), args).output().unwrap()
    }
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).unwrap()
}

#[test]
fn both_names_print_the_version() {
    for exe in [env!("CARGO_BIN_EXE_tf-project"), env!("CARGO_BIN_EXE_tfp")] {
        let out = Command::new(exe).arg("--version").output().unwrap();
        assert!(out.status.success());
        assert_eq!(text(&out.stdout), format!("{}\n", env!("CARGO_PKG_VERSION")));
    }
}

#[test]
fn usage_errors_exit_2() {
    let out = Command::new(env!("CARGO_BIN_EXE_tfp")).arg("--nope").output().unwrap();
    assert_eq!(out.status.code(), Some(2));
}

#[test]
fn ctrl_c_waits_for_terraform() {
    let p = Project::new("{}");
    let child = p.command(env!("CARGO_BIN_EXE_tfp"), &["tf", "sleep"]).spawn().unwrap();
    std::thread::sleep(std::time::Duration::from_millis(300));
    // To tfp alone: Terraform keeps running, and tfp exits with its code rather than dying first.
    let killed = Command::new("kill")
        .args(["-INT", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(killed.success());
    assert_eq!(child.wait_with_output().unwrap().status.code(), Some(7));
}
