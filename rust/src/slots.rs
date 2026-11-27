//! Slots: a name bound to one tfvars file, its banner, and its own `TF_DATA_DIR`.
//!
//! A slot is named after its tfvars file's stem unless the banner sets `slot`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::banner::{SlotName, banner_in, find_banner, resolve_backend_config, resolve_slot_name};
use crate::config::Config;
use crate::models::Banner;
use crate::state::read_active;
use crate::{Error, Result, resolve};

/// Everything needed to operate on a slot, from its banner. Paths are absolute.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedSlot {
    /// The slot's name.
    pub name: String,
    /// Whether the banner chose the name.
    pub name_is_explicit: bool,
    /// The tfvars file.
    pub tfvars: PathBuf,
    /// The project.
    pub project: String,
    /// The project's Terraform sources.
    pub source_root: PathBuf,
    /// The banner's environment.
    pub env: BTreeMap<String, String>,
    /// The resolved `-backend-config`.
    pub backend_config: BTreeMap<String, String>,
    /// `tmp/slots/<name>`.
    pub slot_dir: PathBuf,
    /// The banner's `protected`.
    pub protected: bool,
    /// Whether the banner chose the backend `key` (`state_key`, or `key` in its `backend_config`).
    pub key_is_explicit: bool,
}

impl ResolvedSlot {
    /// The slot's `TF_DATA_DIR`.
    #[must_use]
    pub fn data_dir(&self) -> PathBuf {
        self.slot_dir.join(".terraform")
    }
}

/// A directory under `tmp/slots` that no banner names.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OrphanSlot {
    /// The directory's name.
    pub name: String,
    /// The directory.
    pub slot_dir: PathBuf,
}

/// Where the active slot's name came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Source {
    /// `-s` / `--slot`.
    Flag,
    /// `TFP_SLOT`.
    Env,
    /// `tmp/active`.
    File,
}

impl std::fmt::Display for Source {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Flag => "flag",
            Self::Env => "env",
            Self::File => "file",
        })
    }
}

/// The active slot's name and where it came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActiveSlot {
    /// The name.
    pub name: String,
    /// Where it came from.
    pub source: Source,
}

/// The slot a parsed banner describes.
///
/// # Errors
/// The banner's `state_key` is malformed.
pub fn resolve_slot_from_banner(banner: &Banner, tfvars: &Path, config: &Config) -> Result<ResolvedSlot> {
    let SlotName {
        name,
        explicit: name_is_explicit,
    } = resolve_slot_name(banner, tfvars);
    Ok(ResolvedSlot {
        name_is_explicit,
        tfvars: resolve(tfvars),
        project: banner.project.clone(),
        source_root: resolve(&config.terraform_dir.join(&banner.project)),
        env: banner.env.clone(),
        backend_config: resolve_backend_config(banner, tfvars, config)?,
        slot_dir: config.slot_dir(&name),
        protected: banner.protected,
        key_is_explicit: banner.state_key.is_some() || banner.backend_config.contains_key("key"),
        name,
    })
}

/// The slot `arg` names: a tfvars path when [`looks_like_path`], otherwise a slot name.
///
/// # Errors
/// No such file or slot ([`Error::Slot`]), or the file's banner is missing or malformed.
pub fn resolve_slot(arg: &str, config: &Config, cwd: &Path) -> Result<ResolvedSlot> {
    if looks_like_path(arg) {
        let path = crate::absolute(cwd, Path::new(arg));
        if !path.is_file() {
            return Err(Error::Slot(format!("tfvars file not found: {arg}")));
        }
        return resolve_slot_from_banner(&find_banner(&path)?, &path, config);
    }
    let slots = list_slots(config);
    match slots.iter().find(|s| s.name == arg) {
        Some(slot) => Ok(slot.clone()),
        None => Err(no_such_slot(arg, &slots)),
    }
}

fn no_such_slot(name: &str, slots: &[ResolvedSlot]) -> Error {
    let mut names: Vec<&str> = slots.iter().map(|s| s.name.as_str()).collect();
    names.dedup();
    let available = if names.is_empty() {
        "(none)".to_owned()
    } else {
        names.join(", ")
    };
    Error::Slot(format!("no tfvars defines slot {name:?}. Available: {available}"))
}

/// A `*.tfvars` file under `tfvars_dir` that defines no slot.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SkippedTfvars {
    /// The file.
    pub path: PathBuf,
    /// Why: no banner, or what is wrong with it.
    pub reason: String,
}

impl std::fmt::Display for SkippedTfvars {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} ({})", self.path.display(), self.reason)
    }
}

/// What the `*.tfvars` files under `tfvars_dir` define.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotScan {
    /// Every slot, sorted by name.
    pub slots: Vec<ResolvedSlot>,
    /// The files that define none.
    pub skipped: Vec<SkippedTfvars>,
}

/// Every slot the `*.tfvars` banners under `tfvars_dir` define, and the files that define none.
#[must_use]
pub fn scan_slots(config: &Config) -> SlotScan {
    let mut scan = SlotScan {
        slots: Vec::new(),
        skipped: Vec::new(),
    };
    let files = walkdir::WalkDir::new(&config.tfvars_dir)
        .sort_by_file_name()
        .into_iter()
        .filter_map(std::result::Result::ok)
        .filter(|e| e.file_type().is_file() && e.path().extension().is_some_and(|x| x == "tfvars"));
    for entry in files {
        let path = entry.path();
        let slot = std::fs::read_to_string(path)
            .map_err(|e| e.to_string())
            .and_then(|text| match banner_in(&text) {
                None => Err("no banner".to_owned()),
                Some(banner) => banner.map_err(|e| format!("invalid banner: {e}")),
            })
            .and_then(|banner| resolve_slot_from_banner(&banner, path, config).map_err(|e| e.to_string()));
        match slot {
            Ok(slot) => scan.slots.push(slot),
            Err(reason) => scan.skipped.push(SkippedTfvars {
                path: path.to_path_buf(),
                reason,
            }),
        }
    }
    scan.slots.sort_by(|a, b| a.name.cmp(&b.name));
    scan
}

/// Every slot the `*.tfvars` banners under `tfvars_dir` define, sorted by name; see [`scan_slots`] for the files
/// that define none.
#[must_use]
pub fn list_slots(config: &Config) -> Vec<ResolvedSlot> {
    scan_slots(config).slots
}

/// Two slots with the same backend settings, so one remote state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SharedState {
    /// One slot.
    pub first: ResolvedSlot,
    /// The other.
    pub second: ResolvedSlot,
}

impl SharedState {
    /// Whether both banners chose the key: sharing on purpose.
    #[must_use]
    pub fn is_explicit(&self) -> bool {
        self.first.key_is_explicit && self.second.key_is_explicit
    }

    /// The backend `key` they share.
    #[must_use]
    pub fn key(&self) -> &str {
        self.first.backend_config.get("key").map_or("", String::as_str)
    }
}

/// Pairs of differently named slots whose whole `-backend-config` is the same. Only slots with a backend `key`
/// count.
// ponytail: keyless slots (a local backend) aren't compared; their state lives in the project dir, not in config.
#[must_use]
pub fn find_shared_state(slots: &[ResolvedSlot]) -> Vec<SharedState> {
    let mut shared = Vec::new();
    for (i, first) in slots.iter().enumerate() {
        for second in slots.iter().skip(i + 1) {
            if first.name != second.name
                && first.backend_config.contains_key("key")
                && first.backend_config == second.backend_config
            {
                shared.push(SharedState {
                    first: first.clone(),
                    second: second.clone(),
                });
            }
        }
    }
    shared
}

/// Refuse two tfvars files claiming the same slot name, or two slots sharing a remote state that a banner didn't ask
/// for (a default key from the file name).
///
/// # Errors
/// [`Error::Slot`] naming both files.
pub fn validate_unique(slots: &[ResolvedSlot]) -> Result<()> {
    validate_unique_names(slots)?;
    match find_shared_state(slots).into_iter().find(|s| !s.is_explicit()) {
        Some(s) => Err(Error::Slot(format!(
            "slots {:?} and {:?} share one remote state (key {:?}):\n  {}\n  {}\n\
             Set a distinct \"state_key\" in one banner, or the same \"state_key\" in both to share it on purpose.",
            s.first.name,
            s.second.name,
            s.key(),
            s.first.tfvars.display(),
            s.second.tfvars.display()
        ))),
        None => Ok(()),
    }
}

fn validate_unique_names(slots: &[ResolvedSlot]) -> Result<()> {
    let kind = |s: &ResolvedSlot| if s.name_is_explicit { "explicit" } else { "default" };
    let pair = slots.windows(2).find_map(|w| match w {
        [a, b] if a.name == b.name => Some((a, b)),
        _ => None,
    });
    match pair {
        Some((a, b)) => Err(Error::Slot(format!(
            "slot {:?} is defined by 2 tfvars files:\n  {}      ({})\n  {}      ({})\n\
             Set an explicit \"slot\" in one banner, or rename the file.",
            a.name,
            a.tfvars.display(),
            kind(a),
            b.tfvars.display(),
            kind(b)
        ))),
        _ => Ok(()),
    }
}

/// Directories under `tmp/slots` that no banner names.
#[must_use]
pub fn list_orphans(config: &Config) -> Vec<OrphanSlot> {
    let names: Vec<String> = list_slots(config).into_iter().map(|s| s.name).collect();
    let Ok(entries) = std::fs::read_dir(config.slots_dir()) else {
        return Vec::new();
    };
    let mut orphans: Vec<OrphanSlot> = entries
        .filter_map(std::result::Result::ok)
        .filter(|e| e.path().is_dir())
        .map(|e| OrphanSlot {
            name: e.file_name().to_string_lossy().into_owned(),
            slot_dir: e.path(),
        })
        .filter(|o| !names.contains(&o.name))
        .collect();
    orphans.sort_by(|a, b| a.name.cmp(&b.name));
    orphans
}

/// The active slot: `-s` over `TFP_SLOT` over `tmp/active`. The name may not match any banner yet.
///
/// # Errors
/// `tmp/active` exists but cannot be read.
pub fn resolve_active(config: &Config, flag: Option<&str>, env: Option<&str>) -> Result<Option<ActiveSlot>> {
    let named = |name: &str, source| {
        Some(ActiveSlot {
            name: name.to_owned(),
            source,
        })
    };
    Ok(match (flag.filter(|f| !f.is_empty()), env.filter(|e| !e.is_empty())) {
        (Some(flag), _) => named(flag, Source::Flag),
        (None, Some(env)) => named(env, Source::Env),
        (None, None) => read_active(config)?.and_then(|n| named(&n, Source::File)),
    })
}

/// Refuse a slot name no banner defines.
///
/// # Errors
/// [`Error::Slot`] listing the slots there are.
pub fn assert_slot_exists(config: &Config, name: &str) -> Result<()> {
    let slots = list_slots(config);
    if slots.iter().any(|s| s.name == name) {
        Ok(())
    } else {
        Err(no_such_slot(name, &slots))
    }
}

/// Whether `arg` is a tfvars path rather than a slot name: it ends in `.tfvars` or has a path separator.
#[must_use]
pub fn looks_like_path(arg: &str) -> bool {
    arg.ends_with(".tfvars") || arg.contains(['/', '\\'])
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::{
        ActiveSlot, Source, assert_slot_exists, find_shared_state, list_orphans, list_slots, looks_like_path,
        resolve_active, resolve_slot, scan_slots, validate_unique,
    };
    use crate::Error;
    use crate::state::write_active;
    use crate::testing::Project;

    const DEMO: &str = r#"{"header":"terraform","project":"demo"}"#;

    #[test]
    fn slots_from_banners() {
        let p = Project::new("");
        p.tfvars("b", DEMO);
        p.tfvars(
            "nested/a",
            r#"{"header":"terraform","project":"demo","slot":"z","env":{"E":"1"}}"#,
        );
        p.write("tfvars/none.tfvars", "x = 1\n");
        p.write("tfvars/notes.txt", "# {}\n");
        p.tfvars("badkey", r#"{"header":"terraform","project":"demo","state_key":"rel"}"#);
        let c = p.config();
        let slots = list_slots(&c);
        assert_eq!(slots.iter().map(|s| s.name.as_str()).collect::<Vec<_>>(), ["b", "z"]);
        let z = &slots[1];
        assert!(z.name_is_explicit);
        assert_eq!(z.tfvars, p.path("tfvars/nested/a.tfvars"));
        assert_eq!(z.source_root, p.path("terraform/demo"));
        assert_eq!(z.data_dir(), p.path("tmp/slots/z/.terraform"));
        assert_eq!(z.env["E"], "1");
        assert_eq!(validate_unique(&slots), Ok(()));
        assert_eq!(assert_slot_exists(&c, "z"), Ok(()));
        assert!(matches!(assert_slot_exists(&c, "q"), Err(Error::Slot(m)) if m.ends_with("Available: b, z")));
    }

    #[test]
    fn files_that_define_no_slot_say_why() {
        let p = Project::new("");
        p.tfvars("ok", DEMO);
        p.write("tfvars/none.tfvars", "x = 1\n");
        p.tfvars("broken", r#"{"header":"terraform"}"#);
        p.tfvars("badkey", r#"{"header":"terraform","project":"demo","state_key":"rel"}"#);
        std::fs::write(p.path("tfvars/binary.tfvars"), [0xff, 0xfe]).unwrap();
        let scan = scan_slots(&p.config());
        assert_eq!(scan.slots.len(), 1);
        let reasons: Vec<String> = scan.skipped.iter().map(ToString::to_string).collect();
        assert_eq!(reasons.len(), 4, "{reasons:?}");
        assert!(
            reasons[0].contains("badkey.tfvars (banner in ") && reasons[0].contains("must be an absolute POSIX path"),
            "{reasons:?}"
        );
        assert!(
            reasons[1].contains("binary.tfvars (stream did not contain valid UTF-8)"),
            "{reasons:?}"
        );
        assert!(
            reasons[2].contains("broken.tfvars (invalid banner: missing field `project`"),
            "{reasons:?}"
        );
        assert_eq!(
            reasons[3],
            format!("{} (no banner)", p.path("tfvars/none.tfvars").display())
        );
    }

    #[test]
    fn slots_sharing_a_remote_state() {
        let p = Project::new("state_key_prefix = \"p/\"");
        let c = p.config();
        // Two files named dev.tfvars default to the same key.
        p.tfvars("a/dev", r#"{"header":"terraform","project":"demo","slot":"a"}"#);
        p.tfvars("b/dev", r#"{"header":"terraform","project":"demo","slot":"b"}"#);
        let err = validate_unique(&list_slots(&c)).unwrap_err().to_string();
        assert!(
            err.starts_with("slots \"a\" and \"b\" share one remote state (key \"p/dev.tfstate\"):\n  "),
            "{err}"
        );
        assert!(
            err.contains("a/dev.tfvars\n  ") && err.ends_with("to share it on purpose."),
            "{err}"
        );
        // A different container is a different state.
        p.tfvars(
            "b/dev",
            r#"{"header":"terraform","project":"demo","slot":"b","backend_config":{"container_name":"c2"}}"#,
        );
        assert_eq!(validate_unique(&list_slots(&c)), Ok(()));
        // One banner choosing the other's default key is still an accident.
        p.tfvars(
            "b/dev",
            r#"{"header":"terraform","project":"demo","slot":"b","state_key":"/p/dev.tfstate"}"#,
        );
        assert!(validate_unique(&list_slots(&c)).is_err());
        // Both choosing it is on purpose.
        p.tfvars(
            "a/dev",
            r#"{"header":"terraform","project":"demo","slot":"a","state_key":"/p/dev.tfstate"}"#,
        );
        let slots = list_slots(&c);
        assert_eq!(validate_unique(&slots), Ok(()));
        let shared = find_shared_state(&slots);
        assert_eq!(shared.len(), 1);
        assert!(shared[0].is_explicit());
        assert_eq!(shared[0].key(), "p/dev.tfstate");
        // Same-named slots are validate_unique's other error, and keyless slots have no remote state to compare.
        assert_eq!(find_shared_state(&[slots[0].clone(), slots[0].clone()]), []);
        let keyless = Project::new("");
        keyless.tfvars("x", DEMO);
        keyless.tfvars("y", DEMO);
        assert_eq!(find_shared_state(&list_slots(&keyless.config())), []);
    }

    #[test]
    fn resolve_by_name_or_path() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        let c = p.config();
        assert_eq!(resolve_slot("dev", &c, &p.root).unwrap().name, "dev");
        assert_eq!(resolve_slot("tfvars/dev.tfvars", &c, &p.root).unwrap().name, "dev");
        assert_eq!(
            resolve_slot("../tfvars/dev.tfvars", &c, &p.path("terraform"))
                .unwrap()
                .name,
            "dev"
        );
        assert_eq!(
            resolve_slot("nope", &c, &p.root),
            Err(Error::Slot("no tfvars defines slot \"nope\". Available: dev".into()))
        );
        assert_eq!(
            resolve_slot("x.tfvars", &c, &p.root),
            Err(Error::Slot("tfvars file not found: x.tfvars".into()))
        );
        p.write("tfvars/plain.tfvars", "x = 1\n");
        assert!(matches!(
            resolve_slot("tfvars/plain.tfvars", &c, &p.root),
            Err(Error::Msg(_))
        ));
        let empty = Project::new("");
        assert!(
            matches!(resolve_slot("x", &empty.config(), &empty.root), Err(Error::Slot(m)) if m.ends_with("(none)"))
        );
    }

    #[test]
    fn conflicts_name_both_files() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        p.tfvars("other", r#"{"header":"terraform","project":"demo","slot":"dev"}"#);
        let err = validate_unique(&list_slots(&p.config())).unwrap_err();
        assert!(matches!(err, Error::Slot(_)));
        let m = err.to_string();
        assert!(m.starts_with("slot \"dev\" is defined by 2 tfvars files:"), "{m}");
        assert!(m.contains("(default)") && m.contains("(explicit)"));
        assert!(resolve_slot("dev", &p.config(), &p.root).is_ok());
    }

    #[test]
    fn orphans() {
        let p = Project::new("");
        p.tfvars("dev", DEMO);
        let c = p.config();
        assert_eq!(list_orphans(&c), []);
        for d in ["tmp/slots/dev", "tmp/slots/old"] {
            std::fs::create_dir_all(p.path(d)).unwrap();
        }
        p.write("tmp/slots/file", "");
        let orphans = list_orphans(&c);
        assert_eq!(orphans.iter().map(|o| o.name.as_str()).collect::<Vec<_>>(), ["old"]);
        assert_eq!(orphans[0].slot_dir, p.path("tmp/slots/old"));
    }

    #[test]
    fn active_precedence() {
        let p = Project::new("");
        let c = p.config();
        let active = |name: &str, source| {
            Some(ActiveSlot {
                name: name.into(),
                source,
            })
        };
        assert_eq!(resolve_active(&c, None, None), Ok(None));
        write_active(&c, "file").unwrap();
        assert_eq!(resolve_active(&c, None, Some("")), Ok(active("file", Source::File)));
        assert_eq!(
            resolve_active(&c, Some(""), Some("env")),
            Ok(active("env", Source::Env))
        );
        assert_eq!(
            resolve_active(&c, Some("flag"), Some("env")),
            Ok(active("flag", Source::Flag))
        );
        assert_eq!(
            [Source::Flag, Source::Env, Source::File].map(|s| s.to_string()),
            ["flag", "env", "file"]
        );
    }

    #[test]
    fn path_heuristic() {
        assert!(looks_like_path("dev.tfvars") && looks_like_path("a/b") && looks_like_path("a\\b"));
        assert!(!looks_like_path("dev"));
    }
}
