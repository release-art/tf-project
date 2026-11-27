//! Finding the JSON banner in a tfvars file, and what it resolves to.
//!
//! A banner is a JSON object in a `#` comment, with `"header": "terraform"`:
//!
//! ```text
//! # {"header":"terraform","project":"api","state_key":"/shared/api.tfstate"}
//! ```

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{Value, json};

use crate::config::Config;
use crate::models::Banner;
use crate::{Error, Result, io_error};

/// The banner in `tfvars`.
///
/// # Errors
/// The file cannot be read, has no banner, or its banner is malformed.
pub fn find_banner(tfvars: &Path) -> Result<Banner> {
    parse_banner(&std::fs::read_to_string(tfvars).map_err(io_error(tfvars))?, tfvars)
}

/// The banner in `text`, read from `tfvars`.
///
/// # Errors
/// There is no banner, or it is malformed.
pub fn parse_banner(text: &str, tfvars: &Path) -> Result<Banner> {
    if let Some(banner) = banner_in(text) {
        return banner.map_err(|e| banner_error(tfvars, &e.to_string()));
    }
    Err(Error::Msg(format!(
        "No banner in {}. Add a comment line like `# {{\"header\": \"terraform\", \"project\": \"<name>\"}}`.",
        tfvars.display()
    )))
}

/// The banner in `text`, parsed; none when no comment line is a banner.
pub fn banner_in(text: &str) -> Option<serde_json::Result<Banner>> {
    text.lines()
        .filter(|l| l.starts_with('#'))
        .filter_map(|line| serde_json::from_str::<Value>(line.trim_start_matches('#').trim()).ok())
        .find(|data| data.get("header").and_then(Value::as_str) == Some("terraform"))
        .map(serde_json::from_value)
}

fn banner_error(tfvars: &Path, message: &str) -> Error {
    Error::Msg(format!("banner in {}: {message}", tfvars.display()))
}

/// A warning naming the banner's unknown fields, if it has any.
#[must_use]
pub fn unknown_fields_warning(banner: &Banner, tfvars: &Path) -> Option<String> {
    (!banner.extra.is_empty()).then(|| {
        let names: Vec<&str> = banner.extra.keys().map(String::as_str).collect();
        format!(
            "warning: banner in {} has unknown fields: {}",
            tfvars.display(),
            names.join(", ")
        )
    })
}

/// The `-backend-config` key/values for the banner, later sources winning: a `key` from `state_key_prefix` and the
/// file's stem, the config's `backend_config`, the banner's `backend_config`, then the banner's `state_key`.
///
/// # Errors
/// The banner's `state_key` is not an absolute, normalised POSIX path.
pub fn resolve_backend_config(banner: &Banner, tfvars: &Path, config: &Config) -> Result<BTreeMap<String, String>> {
    let mut merged = BTreeMap::new();
    if !config.state_key_prefix.is_empty() {
        merged.insert(
            "key".to_owned(),
            format!("{}{}.tfstate", config.state_key_prefix, stem(tfvars)),
        );
    }
    merged.extend(config.backend_config.clone());
    merged.extend(banner.backend_config.clone());
    if let Some(key) = &banner.state_key {
        merged.insert("key".to_owned(), normalize_state_key(key, tfvars)?);
    }
    Ok(merged)
}

/// The backend `key` for a banner's `state_key`: it must be an absolute, already-normalised POSIX path, and loses its
/// leading slash (azurerm prepends the container, so a slash would double up).
fn normalize_state_key(state_key: &str, tfvars: &Path) -> Result<String> {
    let reject = |why: &str| Err(banner_error(tfvars, &format!("state_key {state_key:?} {why}")));
    if state_key.contains('\\') {
        return reject("must be a pure POSIX path (no backslashes).");
    }
    let Some(key) = state_key.strip_prefix('/') else {
        return reject("must be an absolute POSIX path (start with '/').");
    };
    if key.is_empty() {
        return reject("resolves to an empty backend key.");
    }
    let segments: Vec<&str> = key.split('/').collect();
    if segments.iter().any(|s| *s == "." || *s == "..") {
        return reject("must not contain '.' or '..' segments.");
    }
    if segments.iter().any(|s| s.is_empty()) {
        return reject("must be a normalised path (no '//' or trailing '/').");
    }
    Ok(key.to_owned())
}

/// A slot's name, and where it came from.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlotName {
    /// The name.
    pub name: String,
    /// Whether the banner chose it; otherwise it is the tfvars file's stem.
    pub explicit: bool,
}

/// The banner's `slot`, or the tfvars file's stem.
#[must_use]
pub fn resolve_slot_name(banner: &Banner, tfvars: &Path) -> SlotName {
    match &banner.slot {
        Some(name) => SlotName {
            name: name.clone(),
            explicit: true,
        },
        None => SlotName {
            name: stem(tfvars),
            explicit: false,
        },
    }
}

/// The file name without its last extension.
#[must_use]
pub fn stem(path: &Path) -> String {
    path.file_stem().unwrap_or_default().to_string_lossy().into_owned()
}

/// What the banner resolves to, for `tfp self banner check`.
///
/// # Errors
/// As [`resolve_backend_config`].
pub fn render_summary(banner: &Banner, tfvars: &Path, config: &Config) -> Result<Value> {
    let slot = resolve_slot_name(banner, tfvars);
    Ok(json!({
        "tfvars": tfvars.display().to_string(),
        "project": banner.project,
        "slot": slot.name,
        "slot_is_explicit": slot.explicit,
        "source_root": config.terraform_dir.join(&banner.project).display().to_string(),
        "backend_config": resolve_backend_config(banner, tfvars, config)?,
        "env": banner.env,
        "protected": banner.protected,
    }))
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;
    use serde_json::json;

    use super::{find_banner, render_summary, resolve_backend_config, unknown_fields_warning};
    use crate::Error;
    use crate::testing::Project;

    #[test]
    fn finds_the_terraform_banner() {
        let p = Project::new("");
        let f = p.write(
            "tfvars/dev.tfvars",
            "# just a comment\n#{\"header\": \"other\"}\n## {\"header\":\"terraform\",\"project\":\"demo\",\"x\":1}\nx=1\n",
        );
        let b = find_banner(&f).unwrap();
        assert_eq!(b.project, "demo");
        let warning = unknown_fields_warning(&b, &f).unwrap();
        assert_eq!(
            warning,
            format!("warning: banner in {} has unknown fields: x", f.display())
        );
        let clean = p.tfvars("qa", r#"{"header":"terraform","project":"demo"}"#);
        assert_eq!(unknown_fields_warning(&find_banner(&clean).unwrap(), &clean), None);
    }

    #[test]
    fn missing_and_malformed_banners() {
        let p = Project::new("");
        let none = p.write("tfvars/a.tfvars", "x = 1\n");
        assert!(find_banner(&none).unwrap_err().to_string().starts_with("No banner in "));
        let bad = p.tfvars("b", r#"{"header":"terraform"}"#);
        let m = find_banner(&bad).unwrap_err().to_string();
        assert!(
            m.starts_with(&format!("banner in {}: missing field `project`", bad.display())),
            "{m}"
        );
        assert!(find_banner(&p.path("tfvars/missing.tfvars")).is_err());
    }

    fn backend(prefix: &str, banner: &str) -> Result<serde_json::Value, Error> {
        let p = Project::new(&format!(
            "state_key_prefix = \"{prefix}\"\nbackend_config = {{ key = \"c\", a = \"1\" }}"
        ));
        let f = p.tfvars("dev", banner);
        let b = find_banner(&f).unwrap();
        Ok(json!(resolve_backend_config(&b, &f, &p.config())?))
    }

    #[test]
    fn backend_config_precedence() {
        let plain = r#"{"header":"terraform","project":"demo"}"#;
        assert_eq!(backend("", plain).unwrap(), json!({"a": "1", "key": "c"}));
        let p = Project::new("state_key_prefix = \"p/\"");
        let f = p.tfvars("dev", plain);
        assert_eq!(
            json!(resolve_backend_config(&find_banner(&f).unwrap(), &f, &p.config()).unwrap()),
            json!({"key": "p/dev.tfstate"})
        );
        let banner = r#"{"header":"terraform","project":"demo","backend_config":{"a":"2","key":"b"},"state_key":"/s/k.tfstate"}"#;
        assert_eq!(backend("p/", banner).unwrap(), json!({"a": "2", "key": "s/k.tfstate"}));
    }

    #[test]
    fn state_key_must_be_clean() {
        for (key, why) in [
            ("a\\\\b", "no backslashes"),
            ("rel/k", "start with '/'"),
            ("/", "empty backend key"),
            ("/a/../b", "'.' or '..'"),
            ("/a//b", "no '//'"),
            ("/a/", "trailing '/'"),
        ] {
            let banner = format!(r#"{{"header":"terraform","project":"demo","state_key":"{key}"}}"#);
            let err = backend("", &banner).unwrap_err().to_string();
            assert!(err.contains(why), "{key}: {err}");
        }
    }

    #[test]
    fn summary() {
        let p = Project::new("");
        let f = p.tfvars(
            "dev",
            r#"{"header":"terraform","project":"demo","slot":"main","env":{"A":"1"}}"#,
        );
        let summary = render_summary(&find_banner(&f).unwrap(), &f, &p.config()).unwrap();
        assert_eq!(summary["slot"], "main");
        assert_eq!(summary["slot_is_explicit"], true);
        assert_eq!(summary["env"], json!({"A": "1"}));
        assert_eq!(summary["protected"], false);
        assert_eq!(summary["source_root"], p.path("terraform/demo").display().to_string());
        let bad = p.tfvars("qa", r#"{"header":"terraform","project":"demo","state_key":"x"}"#);
        assert!(render_summary(&find_banner(&bad).unwrap(), &bad, &p.config()).is_err());
    }
}
