//! The pluggable tfvars preprocessor (by default `op inject`), and shredding its plaintext output.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::{Error, Result};

/// A tfvars file ready to hand to Terraform. A decrypted copy is shredded when this is dropped.
#[derive(Debug)]
pub struct Decrypted {
    path: PathBuf,
    /// The private directory holding a decrypted copy; none when the file is passed through.
    dir: Option<tempfile::TempDir>,
}

impl Decrypted {
    /// The file to pass as `-var-file`.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for Decrypted {
    fn drop(&mut self) {
        if self.dir.is_some() {
            shred(&self.path);
        }
    }
}

/// Overwrite the file with zeros, then delete it. Best effort: on copy-on-write filesystems the old blocks may
/// survive; the delete is what counts.
fn shred(path: &Path) {
    if let Ok(meta) = std::fs::metadata(path)
        && let Ok(mut file) = std::fs::OpenOptions::new().write(true).open(path)
    {
        let zeros = vec![0u8; usize::try_from(meta.len()).unwrap_or(0)];
        let _ = file.write_all(&zeros).and_then(|()| file.sync_all());
    }
    let _ = std::fs::remove_file(path);
}

/// Run `command` (with `{in}` and `{out}` filled in) to turn `src` into a decrypted copy; with no command, `src`
/// itself.
///
/// # Errors
/// `src` is missing, or the command cannot run or fails.
pub fn materialize(command: &[String], src: &Path, temp_dir: &Path) -> Result<Decrypted> {
    let [program, ..] = command else {
        return Ok(Decrypted {
            path: src.to_path_buf(),
            dir: None,
        });
    };
    if !src.is_file() {
        return Err(Error::Msg(format!("tfvars file not found: {}", src.display())));
    }
    // The output must not exist beforehand: `op inject` asks before overwriting.
    let dir = tempfile::Builder::new()
        .prefix("tfp-secrets-")
        .tempdir_in(temp_dir)
        .map_err(|e| Error::Msg(format!("cannot create a private directory for secrets: {e}")))?;
    let out = dir
        .path()
        .join(format!("{}.decrypted.tfvars", crate::banner::stem(src)));
    let decrypted = Decrypted {
        path: out,
        dir: Some(dir),
    };
    let args: Vec<String> = command
        .iter()
        .map(|a| {
            a.replace("{in}", &src.display().to_string())
                .replace("{out}", &decrypted.path.display().to_string())
        })
        .collect();
    let failed = |why: String| Err(Error::Msg(format!("secrets command `{program}` {why}")));
    match Command::new(program).args(args.iter().skip(1)).status() {
        Ok(status) if status.success() => Ok(decrypted),
        Ok(status) => failed(format!("failed ({status})")),
        Err(e) => failed(format!("could not run: {e}")),
    }
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::materialize;
    use crate::testing::Project;

    fn cmd(args: &[&str]) -> Vec<String> {
        args.iter().map(|s| (*s).to_owned()).collect()
    }

    #[test]
    fn no_command_passes_the_file_through() {
        let p = Project::new("");
        let f = p.write("tfvars/dev.tfvars", "x = 1\n");
        let d = materialize(&[], &f, &std::env::temp_dir()).unwrap();
        assert_eq!(d.path(), f);
        drop(d);
        assert!(f.exists());
    }

    #[test]
    fn decrypts_into_a_private_copy_and_shreds_it() {
        let p = Project::new("");
        let f = p.write("tfvars/dev.tfvars", "secret = 1\n");
        let d = materialize(&cmd(&["cp", "{in}", "{out}"]), &f, &std::env::temp_dir()).unwrap();
        let copy = d.path().to_path_buf();
        assert!(copy.ends_with("dev.decrypted.tfvars"));
        assert_eq!(std::fs::read_to_string(&copy).unwrap(), "secret = 1\n");
        drop(d);
        assert!(!copy.exists());
        // A command that writes nothing leaves nothing to shred.
        drop(materialize(&cmd(&["true"]), &f, &std::env::temp_dir()).unwrap());
    }

    #[test]
    fn failures() {
        let p = Project::new("");
        let f = p.write("tfvars/dev.tfvars", "x = 1\n");
        let err = |c: &[&str], src: &std::path::Path| {
            materialize(&cmd(c), src, &std::env::temp_dir())
                .unwrap_err()
                .to_string()
        };
        assert!(err(&["cp", "{in}", "{out}"], &p.path("nope.tfvars")).starts_with("tfvars file not found: "));
        assert!(err(&["false"], &f).starts_with("secrets command `false` failed ("));
        assert!(err(&["/no/such/op"], &f).starts_with("secrets command `/no/such/op` could not run: "));
        let no_tmp = materialize(&cmd(&["true"]), &f, &p.path("nope"))
            .unwrap_err()
            .to_string();
        assert!(
            no_tmp.starts_with("cannot create a private directory for secrets: "),
            "{no_tmp}"
        );
    }
}
