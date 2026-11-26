//! The on-disk side of slots: file locks, each slot's saved state, and the active-slot pointer.

use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};

use crate::config::Config;
use crate::models::{MyState, Versioned};
use crate::{Error, Result, io_error};

/// `<path>.lock`.
fn lock_path(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(".lock");
    name.into()
}

fn open_lock(path: &Path) -> Result<File> {
    let dir = path.parent().unwrap_or(Path::new("/"));
    std::fs::create_dir_all(dir).map_err(io_error(dir))?;
    // Read access, not append: Windows' LockFileEx refuses an append-only handle (os error 5).
    File::options()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(path)
        .map_err(io_error(path))
}

/// Take the lock at `path`, waiting for it. Released when the returned file is dropped.
///
/// # Errors
/// The lock file cannot be created or locked.
pub fn exclusive_lock(path: &Path) -> Result<File> {
    let file = open_lock(path)?;
    file.lock().map(|()| file).map_err(io_error(path))
}

/// Take the lock at `path` if it is free. Released when the returned file is dropped.
///
/// # Errors
/// Another process holds it, or the lock file cannot be created.
pub fn try_exclusive_lock(path: &Path) -> Result<File> {
    let file = open_lock(path)?;
    // Any failure to take a free lock is, in practice, someone else holding it.
    file.try_lock()
        .map(|()| file)
        .map_err(|_| Error::Msg(format!("Lock held by another process: {}", path.display())))
}

/// The slot's saved state, if it has one.
///
/// # Errors
/// The state file is unreadable or malformed.
pub fn load_state(config: &Config, slot: &str) -> Result<Option<MyState>> {
    let path = config.state_file(slot);
    if !path.exists() {
        return Ok(None);
    }
    Ok(Some(MyState::load_file(&path)?))
}

/// Save `state` as its slot's state, under the state file's lock.
///
/// # Errors
/// The state file cannot be locked or written.
pub fn save_state(config: &Config, state: &MyState) -> Result<()> {
    let path = config.state_file(&state.slot);
    let _lock = exclusive_lock(&lock_path(&path))?;
    Ok(state.dump_file(&path)?)
}

/// The active slot named in `tmp/active`, if any.
///
/// # Errors
/// The file exists but cannot be read.
pub fn read_active(config: &Config) -> Result<Option<String>> {
    let path = config.active_file();
    if !path.exists() {
        return Ok(None);
    }
    let text = std::fs::read_to_string(&path).map_err(io_error(&path))?;
    let name = text.trim();
    Ok((!name.is_empty()).then(|| name.to_owned()))
}

/// Make `slot` the active slot. Atomic: another shell reads the old name or the new one, never half of one.
///
/// # Errors
/// The pointer cannot be locked or written.
pub fn write_active(config: &Config, slot: &str) -> Result<()> {
    let path = config.active_file();
    let _lock = exclusive_lock(&lock_path(&path))?;
    let dir = path.parent().unwrap_or(Path::new("/"));
    tempfile::NamedTempFile::new_in(dir)
        .and_then(|mut tmp| writeln!(tmp, "{slot}").map(|()| tmp))
        .and_then(|tmp| tmp.persist(&path).map_err(Into::into))
        .map(drop)
        .map_err(io_error(&path))
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::{exclusive_lock, load_state, read_active, save_state, try_exclusive_lock, write_active};
    use crate::Error;
    use crate::models::MyState;
    use crate::testing::Project;

    fn state(name: &str) -> MyState {
        MyState {
            slot: name.to_owned(),
            project: "demo".to_owned(),
            tfvars: "/t/dev.tfvars".into(),
            source_root: "/t/src".into(),
            tfplan_location: "/t/plan".into(),
            environ: [("A".into(), "1".into())].into(),
            backend_config: [("key".into(), "k".into())].into(),
        }
    }

    #[test]
    fn state_round_trip() {
        let p = Project::new("");
        let c = p.config();
        assert_eq!(load_state(&c, "dev"), Ok(None));
        save_state(&c, &state("dev")).unwrap();
        assert_eq!(load_state(&c, "dev"), Ok(Some(state("dev"))));
        assert!(c.slot_dir("dev").join("state.json.lock").exists());
        std::fs::write(c.state_file("dev"), "[]").unwrap();
        assert!(matches!(load_state(&c, "dev"), Err(Error::Msg(_))));
    }

    #[test]
    fn active_pointer() {
        let p = Project::new("");
        let c = p.config();
        assert_eq!(read_active(&c), Ok(None));
        write_active(&c, "dev").unwrap();
        assert_eq!(std::fs::read_to_string(c.active_file()).unwrap(), "dev\n");
        assert_eq!(read_active(&c), Ok(Some("dev".into())));
        std::fs::write(c.active_file(), " \n").unwrap();
        assert_eq!(read_active(&c), Ok(None));
        std::fs::remove_file(c.active_file()).unwrap();
        std::fs::create_dir(c.active_file()).unwrap();
        assert!(read_active(&c).is_err());
        assert!(write_active(&c, "dev").is_err());
    }

    #[test]
    fn try_lock_refuses_a_held_lock() {
        let p = Project::new("");
        let path = p.path("tmp/slots/dev/apply.lock");
        let held = exclusive_lock(&path).unwrap();
        let busy = try_exclusive_lock(&path).unwrap_err();
        assert_eq!(
            busy,
            Error::Msg(format!("Lock held by another process: {}", path.display()))
        );
        drop(held);
        drop(try_exclusive_lock(&path).unwrap());
        // A lock file that cannot be created: its directory is a file.
        assert!(try_exclusive_lock(&p.path("tf_project.toml/x.lock")).is_err());
        // A directory where the lock file should be.
        assert!(exclusive_lock(&p.path("tmp/slots")).is_err());
        std::fs::write(p.path("tmp/slots/qa"), "").unwrap();
        assert!(save_state(&p.config(), &state("qa")).is_err());
    }
}
