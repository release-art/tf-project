//! Slots: a name bound to one tfvars file, its banner, and its own `TF_DATA_DIR`.
//!
//! A slot is named after its tfvars file's stem unless the banner sets `slot`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Serialize;

use crate::banner::{SlotName, find_banner, resolve_backend_config, resolve_slot_name};
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

/// Every slot the `*.tfvars` banners under `tfvars_dir` define, sorted by name. Files without a usable banner are
/// skipped; `tfp self banner check` explains them.
#[must_use]
pub fn list_slots(config: &Config) -> Vec<ResolvedSlot> {
    let mut slots: Vec<ResolvedSlot> = walkdir::WalkDir::new(&config.tfvars_dir)
        .sort_by_file_name()
        .into_iter()
        .filter_map(std::result::Result::ok)
        .filter(|e| e.file_type().is_file() && e.path().extension().is_some_and(|x| x == "tfvars"))
        .filter_map(|e| {
            find_banner(e.path())
                .and_then(|b| resolve_slot_from_banner(&b, e.path(), config))
                .ok()
        })
        .collect();
    slots.sort_by(|a, b| a.name.cmp(&b.name));
    slots
}

/// Refuse two tfvars files claiming the same slot name.
///
/// # Errors
/// [`Error::Slot`] naming both files.
pub fn validate_unique(slots: &[ResolvedSlot]) -> Result<()> {
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
        ActiveSlot, Source, assert_slot_exists, list_orphans, list_slots, looks_like_path, resolve_active,
        resolve_slot, validate_unique,
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
