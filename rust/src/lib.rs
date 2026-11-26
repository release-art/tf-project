//! tf-project: a Terraform / OpenTofu wrapper with named per-tfvars slots.
//!
//! The library is the whole program; `src/main.rs` and `src/bin/tfp.rs` are its two names.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

pub mod banner;
pub mod cli;
pub mod commands;
pub mod config;
pub mod lock;
pub mod models;
pub mod secrets;
pub mod self_commands;
pub mod slots;
pub mod state;
pub mod terraform;
pub mod text;

/// Write a line to one of the [`Ctx`] streams. A closed stream is not an error worth failing a command for.
#[macro_export]
macro_rules! say {
    ($stream:expr, $($arg:tt)*) => {{
        let _ = writeln!($stream, $($arg)*);
    }};
}

/// Why a command failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Error {
    /// Terraform, or a check, exited with this code; it has said why.
    Exit(i32),
    /// A slot could not be resolved. Callers fall back on it, so it is told apart.
    Slot(String),
    /// The configuration is missing or malformed.
    Config(String),
    /// Anything else the user should read.
    Msg(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Exit(code) => write!(f, "exit code {code}"),
            Self::Config(m) => write!(f, "config error: {m}"),
            Self::Slot(m) | Self::Msg(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for Error {}

impl From<models::SchemaError> for Error {
    fn from(e: models::SchemaError) -> Self {
        Self::Msg(e.to_string())
    }
}

/// The result of anything that can fail a command.
pub type Result<T> = std::result::Result<T, Error>;

/// An I/O failure on `path`, as an [`Error`].
pub fn io_error(path: &Path) -> impl Fn(std::io::Error) -> Error + '_ {
    move |e| Error::Msg(format!("{}: {e}", path.display()))
}

/// What a command reads from and writes to, besides the files it works on.
pub struct Ctx<'a> {
    /// Standard output.
    pub out: &'a mut dyn Write,
    /// Standard error.
    pub err: &'a mut dyn Write,
    /// Standard input, for confirmations.
    pub input: &'a mut dyn BufRead,
    /// The directory tfp was started in.
    pub cwd: PathBuf,
    /// `-s` / `--slot`.
    pub slot_flag: Option<String>,
    /// `TFP_SLOT`.
    pub slot_env: Option<String>,
    /// How Terraform is run.
    pub terraform: terraform::Options,
    /// `PATH` for the cloud CLIs tfp starts; tfp's own when none.
    pub search_path: Option<std::ffi::OsString>,
    /// Where decrypted tfvars are written, in a private directory.
    pub temp_dir: PathBuf,
    /// The HTTP client for cloud SDKs (Azure Key Vault); the SDK's own when none.
    pub http: Option<std::sync::Arc<dyn azure_core::http::HttpClient>>,
}

impl Ctx<'_> {
    /// Ask `question`; true on yes. Anything but yes, or the end of input, is no.
    pub fn confirm(&mut self, question: &str) -> bool {
        let _ = write!(self.err, "{question} [y/N]: ");
        let mut answer = String::new();
        let _ = self.input.read_line(&mut answer);
        matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes")
    }
}

/// `path` with symlinks resolved, or as given when it does not exist. `path` must be absolute.
#[must_use]
pub fn resolve(path: &Path) -> PathBuf {
    dunce::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

/// `path`, made absolute against `base` and resolved.
#[must_use]
pub fn absolute(base: &Path, path: &Path) -> PathBuf {
    resolve(&base.join(path))
}

/// Lowercase hexadecimal of `bytes`.
#[must_use]
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |hex, b| hex + &format!("{b:02x}"))
}

#[cfg(test)]
pub(crate) mod testing {
    //! A scratch project and a [`Ctx`] over buffers, for the tests of every module.

    use std::path::{Path, PathBuf};

    use crate::config::Config;
    use crate::{Ctx, terraform};

    /// A project in a temporary directory: `tf_project.toml`, `terraform/demo/`, `tfvars/`.
    pub(crate) struct Project {
        _dir: tempfile::TempDir,
        pub(crate) root: PathBuf,
    }

    impl Project {
        /// A project whose `[tf_project]` table also has `extra`; Terraform is `bin/terraform`.
        pub(crate) fn new(extra: &str) -> Self {
            let dir = tempfile::tempdir().unwrap();
            let root = crate::resolve(dir.path());
            let project = Self { _dir: dir, root };
            project.write(
                "tf_project.toml",
                &format!("[tf_project]\nterraform_binary = \"bin/terraform\"\n{extra}\n"),
            );
            std::fs::create_dir_all(project.root.join("terraform/demo")).unwrap();
            std::fs::create_dir_all(project.root.join("tfvars")).unwrap();
            project
        }

        pub(crate) fn path(&self, rel: &str) -> PathBuf {
            self.root.join(rel)
        }

        pub(crate) fn write(&self, rel: &str, text: &str) -> PathBuf {
            let path = self.path(rel);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, text).unwrap();
            path
        }

        /// `tfvars/<name>.tfvars` with `banner` as its JSON banner.
        pub(crate) fn tfvars(&self, name: &str, banner: &str) -> PathBuf {
            self.write(&format!("tfvars/{name}.tfvars"), &format!("# {banner}\nx = 1\n"))
        }

        pub(crate) fn config(&self) -> Config {
            Config::discover(&self.root).unwrap()
        }

        /// A fake terraform: a script that logs its arguments to `terraform.log`, then runs `body`.
        pub(crate) fn fake_terraform(&self, body: &str) -> PathBuf {
            use std::os::unix::fs::PermissionsExt;
            let log = self.path("terraform.log");
            let path = self.write(
                "bin/terraform",
                &format!("#!/bin/sh\necho \"$@\" >> '{}'\n{body}\n", log.display()),
            );
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
            path
        }

        /// What the fake terraform was called with, one line per call.
        pub(crate) fn terraform_calls(&self) -> Vec<String> {
            std::fs::read_to_string(self.path("terraform.log"))
                .unwrap_or_default()
                .lines()
                .map(str::to_owned)
                .collect()
        }
    }

    /// Buffers for a [`Ctx`]'s streams.
    #[derive(Default)]
    pub(crate) struct Streams {
        pub(crate) out: Vec<u8>,
        pub(crate) err: Vec<u8>,
        pub(crate) input: std::io::Cursor<Vec<u8>>,
    }

    impl Streams {
        pub(crate) fn with_input(input: &str) -> Self {
            Self {
                input: std::io::Cursor::new(input.as_bytes().to_vec()),
                ..Self::default()
            }
        }

        pub(crate) fn ctx(&mut self, cwd: &Path) -> Ctx<'_> {
            Ctx {
                out: &mut self.out,
                err: &mut self.err,
                input: &mut self.input,
                cwd: cwd.to_path_buf(),
                slot_flag: None,
                slot_env: None,
                terraform: terraform::Options::default(),
                search_path: None,
                temp_dir: std::env::temp_dir(),
                http: None,
            }
        }

        pub(crate) fn out(&self) -> String {
            String::from_utf8(self.out.clone()).unwrap()
        }

        pub(crate) fn err(&self) -> String {
            String::from_utf8(self.err.clone()).unwrap()
        }
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use pretty_assertions::assert_eq;

    use super::Error;
    use super::testing::Streams;

    #[test]
    fn errors_read_as_the_python_cli_printed_them() {
        assert_eq!(Error::Exit(3).to_string(), "exit code 3");
        assert_eq!(Error::Config("bad".into()).to_string(), "config error: bad");
        assert_eq!(Error::Slot("s".into()).to_string(), "s");
        let schema = crate::models::SchemaError::NoVersion("/x".into());
        assert_eq!(
            Error::from(schema).to_string(),
            "/x: missing or non-int `schema_version`"
        );
        let io = super::io_error(Path::new("/p"))(std::io::ErrorKind::NotFound.into());
        assert!(io.to_string().starts_with("/p: "));
    }

    #[test]
    fn a_full_stream_is_not_fatal() {
        let mut s = Streams::default();
        let mut ctx = s.ctx(Path::new("/"));
        let mut full: &mut [u8] = &mut [];
        ctx.out = &mut full;
        say!(ctx.out, "dropped");
        say!(ctx.err, "kept");
        assert_eq!(s.err(), "kept\n");
    }

    #[test]
    fn confirm_takes_only_yes() {
        for (input, expected) in [
            ("y\n", true),
            ("YES\n", true),
            ("n\n", false),
            ("\n", false),
            ("", false),
        ] {
            let mut s = Streams::with_input(input);
            assert_eq!(s.ctx(Path::new("/")).confirm("Go?"), expected, "{input:?}");
            assert_eq!(s.err(), "Go? [y/N]: ");
        }
    }

    #[test]
    fn hex_and_paths() {
        assert_eq!(super::hex(&[0, 255, 16]), "00ff10");
        assert_eq!(super::resolve(Path::new("/no/such/dir")), Path::new("/no/such/dir"));
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(super::absolute(dir.path(), Path::new(".")), super::resolve(dir.path()));
    }
}
