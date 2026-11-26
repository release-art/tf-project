//! Everything that crosses the disk boundary: the versioned JSON files under `tmp_dir` and the tfvars banner.
//!
//! The files are byte-for-byte what the Python tf-project wrote (`tests/fixtures`), so either can read the other's.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::Value;

fn check_non_empty<E: serde::de::Error>(s: String) -> Result<String, E> {
    if s.is_empty() {
        Err(E::custom("String should have at least 1 character"))
    } else {
        Ok(s)
    }
}

/// A string that must not be empty.
fn non_empty<'de, D: Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    check_non_empty(String::deserialize(d)?)
}

/// An optional string that, when given, must not be empty.
fn non_empty_opt<'de, D: Deserializer<'de>>(d: D) -> Result<Option<String>, D::Error> {
    Option::<String>::deserialize(d)?.map(check_non_empty).transpose()
}

/// A versioned file could not be read or written.
#[derive(Debug)]
pub enum SchemaError {
    /// The file could not be read, or written.
    Io(PathBuf, std::io::Error),
    /// The file is not JSON, or its fields do not match the model.
    Invalid(PathBuf, serde_json::Error),
    /// The top level is not a JSON object.
    NotAnObject(PathBuf),
    /// `schema_version` is missing or not an integer.
    NoVersion(PathBuf),
    /// Written by a newer tf-project than this one.
    TooNew {
        /// The file.
        path: PathBuf,
        /// Its `schema_version`.
        found: i64,
        /// The newest version this binary reads.
        supported: i64,
    },
}

impl std::fmt::Display for SchemaError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(path, e) => write!(f, "{}: {e}", path.display()),
            Self::Invalid(path, e) => write!(f, "{}: {e}", path.display()),
            Self::NotAnObject(path) => write!(f, "{}: expected a JSON object at the top level", path.display()),
            Self::NoVersion(path) => write!(f, "{}: missing or non-int `schema_version`", path.display()),
            Self::TooNew { path, found, supported } => write!(
                f,
                "{}: schema_version={found} was written by a newer tf-project; \
                 this binary supports up to v{supported}. Upgrade tfp.",
                path.display()
            ),
        }
    }
}

impl std::error::Error for SchemaError {}

/// A model with its `schema_version` first, as stored.
#[derive(Serialize)]
struct Envelope<'a, T> {
    schema_version: i64,
    #[serde(flatten)]
    body: &'a T,
}

/// A model stored as a JSON object carrying an explicit `schema_version`.
///
/// The version lives in the file, not the struct: `load_file` checks and strips it, `dump_file` writes it first.
/// Older versions are read as they are; a breaking change adds its migration to `load_file` when it happens.
pub trait Versioned: Serialize + DeserializeOwned {
    /// The newest `schema_version` this binary reads, and the one it writes.
    const SUPPORTED_VERSION: i64;

    /// Read and validate the file at `path`.
    ///
    /// # Errors
    /// See [`SchemaError`].
    fn load_file(path: &Path) -> Result<Self, SchemaError> {
        let text = std::fs::read(path).map_err(|e| SchemaError::Io(path.into(), e))?;
        let raw: Value = serde_json::from_slice(&text).map_err(|e| SchemaError::Invalid(path.into(), e))?;
        let Value::Object(mut fields) = raw else {
            return Err(SchemaError::NotAnObject(path.into()));
        };
        let found = fields.remove("schema_version").as_ref().and_then(Value::as_i64);
        let found = found.ok_or_else(|| SchemaError::NoVersion(path.into()))?;
        if found > Self::SUPPORTED_VERSION {
            return Err(SchemaError::TooNew {
                path: path.into(),
                found,
                supported: Self::SUPPORTED_VERSION,
            });
        }
        serde_json::from_value(Value::Object(fields)).map_err(|e| SchemaError::Invalid(path.into(), e))
    }

    /// The model as stored, `schema_version` included; null if it cannot be represented (a non-Unicode path).
    fn to_json(&self) -> Value {
        serde_json::to_value(Envelope {
            schema_version: Self::SUPPORTED_VERSION,
            body: self,
        })
        .unwrap_or_default()
    }

    /// Write the model to `path` as JSON indented by four spaces, atomically: a reader sees the old file or the new
    /// one, never half of one. The parent directory is created if missing.
    ///
    /// # Errors
    /// The directory or the file cannot be written.
    fn dump_file(&self, path: &Path) -> Result<(), SchemaError> {
        let mut buf = Vec::new();
        let mut ser =
            serde_json::Serializer::with_formatter(&mut buf, serde_json::ser::PrettyFormatter::with_indent(b"    "));
        Envelope {
            schema_version: Self::SUPPORTED_VERSION,
            body: self,
        }
        .serialize(&mut ser)
        .map_err(|e| SchemaError::Invalid(path.into(), e))?;
        buf.push(b'\n');
        let dir = path.parent().unwrap_or(Path::new("."));
        std::fs::create_dir_all(dir)
            .and_then(|()| tempfile::NamedTempFile::new_in(dir))
            .and_then(|mut tmp| std::io::Write::write_all(&mut tmp, &buf).map(|()| tmp))
            .and_then(|tmp| tmp.persist(path).map_err(Into::into))
            .map(drop)
            .map_err(|e| SchemaError::Io(path.into(), e))
    }
}

/// Per-slot init state, `slots/<slot>/state.json`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MyState {
    /// The slot's name.
    #[serde(deserialize_with = "non_empty")]
    pub slot: String,
    /// The project, a directory under `terraform_dir`.
    #[serde(deserialize_with = "non_empty")]
    pub project: String,
    /// The slot's tfvars file.
    pub tfvars: PathBuf,
    /// The project's Terraform sources.
    pub source_root: PathBuf,
    /// Where `plan` saves the plan and `apply` reads it.
    pub tfplan_location: PathBuf,
    /// Extra environment for Terraform.
    #[serde(default)]
    pub environ: BTreeMap<String, String>,
    /// `-backend-config` key/values.
    #[serde(default)]
    pub backend_config: BTreeMap<String, String>,
}

impl Versioned for MyState {
    const SUPPORTED_VERSION: i64 = 1;
}

/// The sidecar next to a saved plan: `apply` refuses the plan unless its inputs still match.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TfplanMeta {
    /// SHA-256 over the decrypted tfvars and every `.tf` under `source_root`.
    pub inputs_sha256: String,
    /// The banner's project at plan time.
    pub project: String,
    /// The banner's `state_key` at plan time.
    #[serde(default)]
    pub state_key: Option<String>,
    /// The backend configuration at plan time.
    #[serde(default)]
    pub backend_config: BTreeMap<String, String>,
    /// The banner's environment at plan time.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
}

impl Versioned for TfplanMeta {
    const SUPPORTED_VERSION: i64 = 1;
}

/// The last Terraform invocation, shown by `tfp last`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LastInvocation {
    /// When it ran.
    pub timestamp: String,
    /// The command line.
    pub argv: Vec<String>,
    /// Its exit code; none for a passthrough, which replaced tfp before it could know.
    #[serde(default)]
    pub exit_code: Option<i64>,
}

impl Versioned for LastInvocation {
    const SUPPORTED_VERSION: i64 = 1;
}

/// The only accepted `header` of a banner.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum Header {
    /// `"terraform"`.
    #[serde(rename = "terraform")]
    Terraform,
}

/// The JSON banner a tfvars file carries in a `#` comment. Unknown fields are kept in `extra`, for a warning.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Banner {
    /// Always `"terraform"`.
    pub header: Header,
    /// The project, a directory under `terraform_dir`.
    #[serde(deserialize_with = "non_empty")]
    pub project: String,
    /// The slot name; the tfvars file's stem when absent.
    #[serde(default, deserialize_with = "non_empty_opt")]
    pub slot: Option<String>,
    /// The backend `key`, an absolute POSIX path.
    #[serde(default)]
    pub state_key: Option<String>,
    /// Extra environment for Terraform.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// `-backend-config` key/values.
    #[serde(default)]
    pub backend_config: BTreeMap<String, String>,
    /// Settings for each secret source, by URI scheme: `{"az-kv": {"tenant": "…"}}`.
    #[serde(default)]
    pub secrets_config: BTreeMap<String, BTreeMap<String, String>>,
    /// Fields the banner has that tf-project does not know.
    #[serde(flatten)]
    pub extra: BTreeMap<String, Value>,
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use pretty_assertions::assert_eq;
    use serde_json::json;

    use super::{Banner, LastInvocation, MyState, SchemaError, TfplanMeta, Versioned};

    /// Load a Python-written fixture, write it back, and expect the same bytes.
    fn round_trip<T: Versioned + PartialEq + std::fmt::Debug>(name: &str) -> T {
        let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures").join(name);
        let loaded = T::load_file(&fixture).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let out = dir.path().join("new/dir").join(name);
        loaded.dump_file(&out).unwrap();
        assert_eq!(
            std::fs::read_to_string(&out).unwrap(),
            std::fs::read_to_string(&fixture).unwrap()
        );
        assert_eq!(T::load_file(&out).unwrap(), loaded);
        loaded
    }

    #[test]
    fn python_files_round_trip_byte_for_byte() {
        let state: MyState = round_trip("my_state.json");
        assert_eq!(state.slot, "dev");
        assert_eq!(round_trip::<TfplanMeta>("tfplan_meta.json").state_key, None);
        assert_eq!(round_trip::<LastInvocation>("last_invocation.json").exit_code, None);
    }

    fn load_json(value: &serde_json::Value) -> Result<MyState, SchemaError> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        std::fs::write(&path, value.to_string()).unwrap();
        MyState::load_file(&path)
    }

    fn state(overrides: &serde_json::Value) -> serde_json::Value {
        let mut base = json!({
            "schema_version": 1, "slot": "dev", "project": "demo", "tfvars": "/t/dev.tfvars",
            "source_root": "/t/src", "tfplan_location": "/t/dev/tfplan",
        });
        for (k, v) in overrides.as_object().unwrap() {
            base[k] = v.clone();
        }
        base
    }

    fn error(value: &serde_json::Value) -> String {
        load_json(value).unwrap_err().to_string()
    }

    #[test]
    fn defaults_fill_missing_maps() {
        assert!(load_json(&state(&json!({}))).unwrap().environ.is_empty());
    }

    #[test]
    fn older_version_is_read() {
        assert!(load_json(&state(&json!({"schema_version": 0}))).is_ok());
    }

    #[test]
    fn rejects_bad_files() {
        assert!(error(&state(&json!({"unknown": "oops"}))).contains("unknown field `unknown`"));
        assert!(error(&state(&json!({"slot": ""}))).contains("at least 1 character"));
        assert!(error(&state(&json!({"schema_version": "1"}))).ends_with("missing or non-int `schema_version`"));
        assert!(error(&state(&json!({"schema_version": 999}))).ends_with(
            "schema_version=999 was written by a newer tf-project; this binary supports up to v1. Upgrade tfp."
        ));
        assert!(error(&json!([])).ends_with("expected a JSON object at the top level"));
    }

    #[test]
    fn unreadable_and_unwritable_paths() {
        let dir = tempfile::tempdir().unwrap();
        let missing = dir.path().join("missing.json");
        assert!(matches!(MyState::load_file(&missing), Err(SchemaError::Io(..))));
        std::fs::write(&missing, "not json").unwrap();
        assert!(matches!(MyState::load_file(&missing), Err(SchemaError::Invalid(..))));
        let state = load_json(&state(&json!({}))).unwrap();
        // A file where the parent directory should be.
        let blocked = missing.join("state.json");
        assert!(
            state
                .dump_file(&blocked)
                .unwrap_err()
                .to_string()
                .starts_with(&blocked.display().to_string())
        );
        // A directory where the file should be.
        assert!(matches!(state.dump_file(dir.path()), Err(SchemaError::Io(..))));
    }

    #[test]
    fn a_path_that_is_not_unicode_cannot_be_written() {
        let mut state = load_json(&state(&json!({}))).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::ffi::OsStringExt;
            state.tfvars = std::ffi::OsString::from_vec(vec![0xff]).into();
        }
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStringExt;
            state.tfvars = std::ffi::OsString::from_wide(&[0xd800]).into();
        }
        let dir = tempfile::tempdir().unwrap();
        assert!(matches!(
            state.dump_file(&dir.path().join("s.json")),
            Err(SchemaError::Invalid(..))
        ));
    }

    fn banner(value: &serde_json::Value) -> Result<Banner, serde_json::Error> {
        serde_json::from_value(value.clone())
    }

    #[test]
    fn banner_keeps_unknown_fields() {
        let b = banner(&json!({"header": "terraform", "project": "api", "owner": "me"})).unwrap();
        assert_eq!(b.project, "api");
        assert_eq!(b.slot, None);
        assert_eq!(b.extra.keys().collect::<Vec<_>>(), ["owner"]);
    }

    #[test]
    fn banner_rejects() {
        assert!(banner(&json!({"header": "tofu", "project": "api"})).is_err());
        assert!(banner(&json!({"header": "terraform"})).is_err());
        assert!(banner(&json!({"header": "terraform", "project": "api", "slot": ""})).is_err());
        assert!(banner(&json!({"header": "terraform", "project": "api", "slot": null})).is_ok());
        assert!(banner(&json!({"header": "terraform", "project": 1})).is_err());
        assert!(banner(&json!({"header": "terraform", "project": "api", "slot": 1})).is_err());
        assert!(banner(&json!({"header": "terraform", "project": "api", "env": {"A": 1}})).is_err());
    }
}
