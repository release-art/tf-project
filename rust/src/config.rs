//! Finding and loading the `[tf_project]` configuration. Every path in a loaded [`Config`] is absolute.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::{Error, Result, absolute, io_error, resolve};

/// The standalone config file.
pub const CONFIG_FILE_NAME: &str = "tf_project.toml";
/// The config file's table.
pub const CONFIG_TABLE: &str = "tf_project";
/// The Python project file that may carry the config instead.
pub const PYPROJECT_FILE_NAME: &str = "pyproject.toml";
/// Its table.
pub const PYPROJECT_TABLE: [&str; 2] = ["tool", "tf_project"];

/// The configuration, resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// The directory holding the config file.
    pub project_root: PathBuf,
    /// Terraform projects, one directory each.
    pub terraform_dir: PathBuf,
    /// The tfvars files, searched recursively.
    pub tfvars_dir: PathBuf,
    /// Slot state, plans, snapshots and the plugin cache.
    pub tmp_dir: PathBuf,
    /// Prefix of the backend `key` synthesised from a tfvars file's stem.
    pub state_key_prefix: String,
    /// The Terraform (or OpenTofu) program: an absolute path, or a name looked up on `PATH`.
    pub terraform_binary: String,
    /// `-backend-config` key/values for every slot.
    pub backend_config: BTreeMap<String, String>,
    /// The other programs tfp runs.
    pub programs: Programs,
    /// Whether the config still has the old `[tf_project.secrets] command`, which is no longer used.
    pub legacy_secrets_command: bool,
    /// `TF_PLUGIN_CACHE_DIR`; none when disabled with `plugin_cache_dir = ""`.
    pub plugin_cache_dir: Option<PathBuf>,
    /// `GOOS_GOARCH` platforms locked on top of the defaults.
    pub extra_lock_platforms: Vec<String>,
    /// Auto-snapshots kept per slot; 0 disables them.
    pub snapshot_retention: u32,
}

/// The table as written.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Table {
    #[serde(default = "default_terraform_dir")]
    terraform_dir: PathBuf,
    #[serde(default = "default_tfvars_dir")]
    tfvars_dir: PathBuf,
    #[serde(default = "default_tmp_dir")]
    tmp_dir: PathBuf,
    #[serde(default)]
    state_key_prefix: String,
    terraform_binary: Option<String>,
    #[serde(default)]
    backend_config: BTreeMap<String, String>,
    #[serde(default)]
    secrets: Secrets,
    #[serde(default)]
    programs: ProgramsTable,
    plugin_cache_dir: Option<PathBuf>,
    #[serde(default)]
    extra_lock_platforms: Vec<String>,
    #[serde(default = "default_retention")]
    snapshot_retention: u32,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Secrets {
    command: Option<Vec<String>>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProgramsTable {
    op: Option<String>,
    az: Option<String>,
    aws: Option<String>,
}

/// The programs tfp runs besides Terraform: names looked up on `PATH`, or absolute paths.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Programs {
    /// The 1Password CLI, for `op://` secrets.
    pub op: String,
    /// The Azure CLI, for `az-kv://` secrets and the azurerm lock commands.
    pub az: String,
    /// The AWS CLI, for the S3 lock commands.
    pub aws: String,
}

/// A program setting: `default()` when unset; a path (it has a separator) made absolute against `root`; or a name,
/// kept for a `PATH` lookup when run.
fn program(key: &str, value: Option<String>, default: impl FnOnce() -> String, root: &Path) -> Result<String> {
    match value {
        None => Ok(default()),
        Some(v) if v.is_empty() => Err(Error::Config(format!("`{key}` must be a non-empty string"))),
        Some(v) if v.contains(['/', '\\']) => Ok(absolute(root, Path::new(&v)).display().to_string()),
        Some(v) => Ok(v),
    }
}

fn default_terraform_dir() -> PathBuf {
    "terraform".into()
}

fn default_tfvars_dir() -> PathBuf {
    "tfvars".into()
}

fn default_tmp_dir() -> PathBuf {
    "tmp".into()
}

fn default_retention() -> u32 {
    200
}

/// `terraform` as found on `path`, or the bare name for the run to fail on.
fn default_terraform(path: Option<std::ffi::OsString>, cwd: &Path) -> String {
    which::which_in("terraform", path, cwd).map_or_else(|_| "terraform".to_owned(), |p| p.display().to_string())
}

/// Where the config was found.
#[derive(Debug, Clone, PartialEq)]
pub struct Located {
    /// The file.
    pub file: PathBuf,
    /// Its `tf_project` table; none when a `tf_project.toml` lacks one.
    pub table: Option<toml::Table>,
}

/// The nearest config from `start` upwards: any `tf_project.toml` first, then a `pyproject.toml` with
/// `[tool.tf_project]`.
///
/// # Errors
/// A config file that cannot be read or is not TOML.
pub fn locate(start: &Path) -> Result<Option<Located>> {
    let start = resolve(start);
    for dir in start.ancestors() {
        let file = dir.join(CONFIG_FILE_NAME);
        if file.is_file() {
            let table = walk(read_toml(&file)?, &[CONFIG_TABLE]);
            return Ok(Some(Located { file, table }));
        }
    }
    for dir in start.ancestors() {
        let file = dir.join(PYPROJECT_FILE_NAME);
        if file.is_file()
            && let Some(table) = walk(read_toml(&file)?, &PYPROJECT_TABLE)
        {
            return Ok(Some(Located {
                file,
                table: Some(table),
            }));
        }
    }
    Ok(None)
}

/// The file parsed as TOML.
///
/// # Errors
/// It cannot be read or is not TOML.
pub fn read_toml(file: &Path) -> Result<toml::Table> {
    parse_toml(file, &std::fs::read_to_string(file).map_err(io_error(file))?)
}

/// `text`, read from `file`, parsed as TOML.
///
/// # Errors
/// It is not TOML.
pub fn parse_toml(file: &Path, text: &str) -> Result<toml::Table> {
    text.parse()
        .map_err(|e| Error::Config(format!("{}: {e}", file.display())))
}

/// The table at `keys` inside `data`, if every step is a table.
#[must_use]
pub fn walk(data: toml::Table, keys: &[&str]) -> Option<toml::Table> {
    let mut cursor = data;
    for key in keys {
        match cursor.remove(*key) {
            Some(toml::Value::Table(t)) => cursor = t,
            _ => return None,
        }
    }
    Some(cursor)
}

impl Config {
    /// Find and load the config from `start` upwards.
    ///
    /// # Errors
    /// No config is found, or it is malformed.
    pub fn discover(start: &Path) -> Result<Self> {
        let Some(Located { file, table }) = locate(start)? else {
            return Err(Error::Config(format!(
                "No {CONFIG_FILE_NAME} (or [tool.tf_project] in pyproject.toml) in {} or above. \
                 Run `tfp self init` at your project root to create one.",
                resolve(start).display()
            )));
        };
        let table =
            table.ok_or_else(|| Error::Config(format!("Missing [{CONFIG_TABLE}] table in {}", file.display())))?;
        let root = file.parent().unwrap_or(Path::new("/")).to_path_buf();
        Self::from_table(table, &root)
    }

    /// The config in `table`, its relative paths taken from `root`.
    ///
    /// # Errors
    /// The table has unknown keys, wrong types or bad values.
    pub fn from_table(table: toml::Table, root: &Path) -> Result<Self> {
        let raw: Table = table
            .try_into()
            .map_err(|e: toml::de::Error| Error::Config(e.message().to_owned()))?;
        for platform in &raw.extra_lock_platforms {
            let valid = platform.split_once('_').is_some_and(|(os, arch)| {
                let ok = |s: &str| !s.is_empty() && s.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit());
                ok(os) && ok(arch)
            });
            if !valid {
                return Err(Error::Config(format!(
                    "`extra_lock_platforms` entries must look like `<goos>_<goarch>` (e.g. windows_amd64); got {platform:?}"
                )));
            }
        }
        let root = resolve(root);
        let tmp_dir = absolute(&root, &raw.tmp_dir);
        let plugin_cache_dir = match raw.plugin_cache_dir {
            Some(p) if p.as_os_str().is_empty() => None,
            Some(p) => Some(absolute(&root, &p)),
            None => Some(tmp_dir.join("plugin-cache")),
        };
        let terraform_binary = program(
            "terraform_binary",
            raw.terraform_binary,
            || default_terraform(std::env::var_os("PATH"), &root),
            &root,
        )?;
        // The old secrets command may name a custom `op`: keep using it until `programs.op` is set.
        let legacy_op = raw
            .secrets
            .command
            .as_ref()
            .and_then(|c| c.first())
            .filter(|p| p.as_str() != "op" && Path::new(p).file_stem().is_some_and(|s| s == "op"));
        let programs = Programs {
            op: program(
                "programs.op",
                raw.programs.op.or(legacy_op.cloned()),
                || "op".into(),
                &root,
            )?,
            az: program("programs.az", raw.programs.az, || "az".into(), &root)?,
            aws: program("programs.aws", raw.programs.aws, || "aws".into(), &root)?,
        };
        Ok(Self {
            terraform_dir: absolute(&root, &raw.terraform_dir),
            tfvars_dir: absolute(&root, &raw.tfvars_dir),
            tmp_dir,
            project_root: root,
            state_key_prefix: raw.state_key_prefix,
            terraform_binary,
            backend_config: raw.backend_config,
            programs,
            legacy_secrets_command: raw.secrets.command.is_some(),
            plugin_cache_dir,
            extra_lock_platforms: raw.extra_lock_platforms,
            snapshot_retention: raw.snapshot_retention,
        })
    }

    // The layout under tmp_dir:
    //
    //   active                  name of the active slot
    //   plugin-cache/           TF_PLUGIN_CACHE_DIR, shared by all slots
    //   snapshots/<slot>/       remote tfstate snapshots
    //   slots/<slot>/
    //     state.json            MyState
    //     state.json.lock       lock for state.json
    //     backend.hash          signature of the backend the slot was initialised against
    //     tfplan                the plan `tfp plan` saved
    //     tfplan.meta.json      its apply-safety sidecar
    //     last.json             the last Terraform invocation
    //     apply.lock            held while apply/refresh/destroy run
    //     .terraform/           TF_DATA_DIR

    /// `tmp/slots`.
    #[must_use]
    pub fn slots_dir(&self) -> PathBuf {
        self.tmp_dir.join("slots")
    }

    /// `tmp/active`.
    #[must_use]
    pub fn active_file(&self) -> PathBuf {
        self.tmp_dir.join("active")
    }

    /// `tmp/slots/<slot>`.
    #[must_use]
    pub fn slot_dir(&self, slot: &str) -> PathBuf {
        self.slots_dir().join(slot)
    }

    /// The slot's `state.json`.
    #[must_use]
    pub fn state_file(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("state.json")
    }

    /// The slot's saved plan.
    #[must_use]
    pub fn tfplan_file(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("tfplan")
    }

    /// The saved plan's sidecar.
    #[must_use]
    pub fn tfplan_meta(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("tfplan.meta.json")
    }

    /// The slot's `TF_DATA_DIR`.
    #[must_use]
    pub fn data_dir(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join(".terraform")
    }

    /// The slot's backend signature.
    #[must_use]
    pub fn backend_hash(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("backend.hash")
    }

    /// The slot's last Terraform invocation.
    #[must_use]
    pub fn last_invocation(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("last.json")
    }

    /// The lock held while apply, refresh and destroy run.
    #[must_use]
    pub fn apply_lock(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join("apply.lock")
    }

    /// The slot's snapshots.
    #[must_use]
    pub fn snapshots_dir(&self, slot: &str) -> PathBuf {
        self.tmp_dir.join("snapshots").join(slot)
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use pretty_assertions::assert_eq;

    use super::{Config, locate};
    use crate::Error;
    use crate::testing::Project;

    fn load(body: &str) -> Result<Config, Error> {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("tf_project.toml"), body).unwrap();
        Config::discover(dir.path())
    }

    fn config_error(body: &str) -> String {
        let err = load(body).unwrap_err();
        assert!(matches!(err, Error::Config(_)), "{err:?}");
        err.to_string().trim_start_matches("config error: ").to_owned()
    }

    #[test]
    fn defaults_are_absolute() {
        let p = Project::new("");
        let c = Config::discover(&p.path("terraform/demo")).unwrap();
        assert_eq!(c.project_root, p.root);
        assert_eq!(c.terraform_dir, p.path("terraform"));
        assert_eq!(c.tmp_dir, p.path("tmp"));
        assert_eq!(c.plugin_cache_dir, Some(p.path("tmp/plugin-cache")));
        assert_eq!(c.snapshot_retention, 200);
        let programs = (c.programs.op.as_str(), c.programs.az.as_str(), c.programs.aws.as_str());
        assert_eq!((programs, c.legacy_secrets_command), (("op", "az", "aws"), false));
        assert_eq!(c.terraform_binary, p.path("bin/terraform").display().to_string());
        assert_eq!(c.slot_dir("dev"), p.path("tmp/slots/dev"));
        for (path, rel) in [
            (c.active_file(), "tmp/active"),
            (c.state_file("dev"), "tmp/slots/dev/state.json"),
            (c.tfplan_file("dev"), "tmp/slots/dev/tfplan"),
            (c.tfplan_meta("dev"), "tmp/slots/dev/tfplan.meta.json"),
            (c.data_dir("dev"), "tmp/slots/dev/.terraform"),
            (c.backend_hash("dev"), "tmp/slots/dev/backend.hash"),
            (c.last_invocation("dev"), "tmp/slots/dev/last.json"),
            (c.apply_lock("dev"), "tmp/slots/dev/apply.lock"),
            (c.snapshots_dir("dev"), "tmp/snapshots/dev"),
        ] {
            assert_eq!(path, p.path(rel));
        }
    }

    #[test]
    fn explicit_values() {
        let c = load(
            "[tf_project]\ntmp_dir = \"/abs/tmp\"\nplugin_cache_dir = \"cache\"\nterraform_binary = \"bin/tofu\"\n\
             state_key_prefix = \"p/\"\nextra_lock_platforms = [\"windows_amd64\"]\nsnapshot_retention = 0\n\
                          backend_config = { a = \"b\" }\n\
             [tf_project.programs]\nop = \"tools/op\"\naz = \"/opt/az/bin/az\"\naws = \"aws2\"\n",
        )
        .unwrap();
        assert_eq!(c.tmp_dir, Path::new("/abs/tmp"));
        assert_eq!(c.plugin_cache_dir, Some(c.project_root.join("cache")));
        assert_eq!(
            c.terraform_binary,
            c.project_root.join("bin/tofu").display().to_string()
        );
        assert_eq!(c.programs.op, c.project_root.join("tools/op").display().to_string());
        assert_eq!(
            (c.programs.az.as_str(), c.programs.aws.as_str()),
            ("/opt/az/bin/az", "aws2")
        );
        assert_eq!(c.snapshot_retention, 0);
        assert_eq!(c.backend_config["a"], "b");
        assert_eq!(c.state_key_prefix, "p/");
        let disabled = load("[tf_project]\nplugin_cache_dir = \"\"\n[tf_project.secrets]\n").unwrap();
        assert_eq!(disabled.plugin_cache_dir, None);
        assert!(!disabled.legacy_secrets_command);
        // The old secrets command is flagged, and lends its `op` path until `programs.op` is set.
        let legacy =
            |command: &str| load(&format!("[tf_project]\n[tf_project.secrets]\ncommand = {command}\n")).unwrap();
        let old = legacy(r#"["/opt/op", "inject"]"#);
        assert_eq!(
            (old.legacy_secrets_command, old.programs.op.as_str()),
            (true, "/opt/op")
        );
        assert_eq!(legacy(r#"["op", "inject"]"#).programs.op, "op");
        assert_eq!(legacy(r#"["sops", "-d"]"#).programs.op, "op");
        assert_eq!(legacy("[]").programs.op, "op");
    }

    #[test]
    fn terraform_binary_found_on_path_or_by_name() {
        let p = Project::new("");
        let fake = p.fake_terraform("");
        let bin = fake.parent().unwrap();
        assert_eq!(
            super::default_terraform(Some(bin.into()), &p.root),
            fake.display().to_string()
        );
        assert_eq!(
            super::default_terraform(Some(p.path("nowhere").into()), &p.root),
            "terraform"
        );
        assert_ne!(load("[tf_project]\n").unwrap().terraform_binary, "");
        assert_eq!(
            load("[tf_project]\nterraform_binary = \"tofu\"\n")
                .unwrap()
                .terraform_binary,
            "tofu"
        );
    }

    #[test]
    fn rejects() {
        assert!(config_error("[tf_project]\nnope = 1\n").contains("unknown field `nope`"));
        assert!(config_error("[tf_project]\nterraform_binary = \"\"\n").contains("non-empty"));
        for program in ["op", "az", "aws"] {
            let body = format!("[tf_project]\n[tf_project.programs]\n{program} = \"\"\n");
            assert_eq!(
                config_error(&body),
                format!("`programs.{program}` must be a non-empty string")
            );
        }
        assert!(
            config_error("[tf_project]\n[tf_project.programs]\ngcloud = \"g\"\n").contains("unknown field `gcloud`")
        );
        assert!(config_error("[tf_project]\nsnapshot_retention = -1\n").contains("invalid value"));
        assert_eq!(
            config_error("[tf_project]\nextra_lock_platforms = [\"Linux\"]\n"),
            "`extra_lock_platforms` entries must look like `<goos>_<goarch>` (e.g. windows_amd64); got \"Linux\""
        );
        for bad in ["linux", "_amd64", "linux_", "linux_amd_64"] {
            let body = format!("[tf_project]\nextra_lock_platforms = [\"{bad}\"]\n");
            assert!(config_error(&body).contains(bad), "{bad}");
        }
        assert!(config_error("not = [toml").contains("tf_project.toml"));
        assert!(config_error("[other]\n").starts_with("Missing [tf_project] table in "));
    }

    #[test]
    fn pyproject_table_and_precedence() {
        let dir = tempfile::tempdir().unwrap();
        let root = crate::resolve(dir.path());
        std::fs::write(root.join("pyproject.toml"), "[tool.other]\n").unwrap();
        let sub = root.join("a/b");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(root.join("a/pyproject.toml"), "[tool.tf_project]\ntmp_dir = \"t\"\n").unwrap();
        assert_eq!(Config::discover(&sub).unwrap().tmp_dir, root.join("a/t"));
        // A pyproject without the table is skipped; a missing one everywhere is an error.
        std::fs::write(root.join("a/pyproject.toml"), "[tool]\ntf_project = 1\n").unwrap();
        assert!(matches!(Config::discover(&sub), Err(Error::Config(m)) if m.starts_with("No tf_project.toml")));
        // tf_project.toml anywhere up the tree wins over a nearer pyproject.
        std::fs::write(root.join("tf_project.toml"), "[tf_project]\n").unwrap();
        assert_eq!(locate(&sub).unwrap().unwrap().file, root.join("tf_project.toml"));
    }

    #[test]
    fn unreadable_config() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir(dir.path().join("pyproject.toml")).unwrap();
        std::fs::write(dir.path().join("pyproject.toml/x"), "").unwrap();
        // A directory named like the file is not a file: skipped.
        assert_eq!(locate(dir.path()).unwrap(), None);
        std::fs::write(dir.path().join("tf_project.toml"), [0xff, 0xfe]).unwrap();
        assert!(matches!(locate(dir.path()), Err(Error::Msg(_))));
    }
}
