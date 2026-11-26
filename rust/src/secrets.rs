//! Secrets in tfvars: `{{ scheme://… }}` references, resolved by the source for their scheme and written into a
//! private copy of the file that is shredded after use.
//!
//! Each scheme's references are fetched together by the scheme's source, configured by the banner's
//! `secrets_config.<scheme>`:
//! - `op://`: 1Password, through one `op inject` call.
//! - `az-kv://`: Azure Key Vault, through the Azure SDK (see `key_vault`).
//!
//! A new source is a new arm in `resolve`.

mod key_vault;

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fmt::Write as _;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

use crate::config::Config;
use crate::models::Banner;
use crate::{Ctx, Error, Result, hex, io_error, say};

/// A tfvars file ready to hand to Terraform. A decrypted copy is shredded when this is dropped.
#[derive(Debug)]
pub struct Decrypted {
    path: PathBuf,
    contents: Vec<u8>,
    /// The private directory holding a decrypted copy; none when the file is passed through.
    dir: Option<tempfile::TempDir>,
}

impl Decrypted {
    /// The file to pass as `-var-file`.
    #[must_use]
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// What the file holds, secrets included.
    #[must_use]
    pub fn contents(&self) -> &[u8] {
        &self.contents
    }

    fn passthrough(src: &Path, contents: Vec<u8>) -> Self {
        Self {
            path: src.to_path_buf(),
            contents,
            dir: None,
        }
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

/// A private directory for decrypted files, under `temp_dir`.
fn private_dir(temp_dir: &Path) -> Result<tempfile::TempDir> {
    tempfile::Builder::new()
        .prefix("tfp-secrets-")
        .tempdir_in(temp_dir)
        .map_err(|e| Error::Msg(format!("cannot create a private directory for secrets: {e}")))
}

/// A `{{ scheme://… }}` reference in a tfvars file.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Reference<'a> {
    /// Where `{{ … }}` sits in the text.
    span: std::ops::Range<usize>,
    /// What is between the braces, trimmed.
    uri: &'a str,
    /// The part of `uri` before `://`.
    scheme: &'a str,
}

/// Every `{{ scheme://… }}` in `text`, in order. Other `{{ … }}` is left alone.
fn references(text: &str) -> Vec<Reference<'_>> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(open) = text.get(from..).and_then(|t| t.find("{{")).map(|i| i + from) {
        let Some(close) = text.get(open..).and_then(|t| t.find("}}")).map(|i| i + open) else {
            break;
        };
        let uri = text.get(open + 2..close).unwrap_or_default().trim();
        let scheme = uri.split_once("://").map_or("", |(s, _)| s);
        let valid_scheme = scheme.starts_with(|c: char| c.is_ascii_alphabetic())
            && scheme.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c));
        // Names may contain spaces (`op://Infra/Fuji non-prod/Client Id`); a reference never spans lines.
        if valid_scheme && !uri.contains(['\n', '\r', '{']) {
            found.push(Reference {
                span: open..close + 2,
                uri,
                scheme,
            });
        }
        from = close + 2;
    }
    found
}

/// The tfvars file for Terraform: `src` with every `{{ scheme://… }}` replaced by its secret, or `src` itself when
/// it has none.
///
/// # Errors
/// `src` cannot be read, a reference's scheme has no source, or a source fails.
pub fn materialize(config: &Config, ctx: &mut Ctx, src: &Path) -> Result<Decrypted> {
    if config.legacy_secrets_command {
        say!(
            ctx.err,
            "warning: [tf_project.secrets] command is no longer used: tfp resolves {{{{ scheme://… }}}} references \
             itself. Delete it; if `op` isn't on PATH, set [tf_project.programs] op = \"<path>\" instead."
        );
    }
    let text = std::fs::read_to_string(src).map_err(io_error(src))?;
    let refs = references(&text);
    if refs.is_empty() {
        return Ok(Decrypted::passthrough(src, text.into_bytes()));
    }
    // A file without its banner (the slot's saved state stands in for it) has no source settings.
    let banner = crate::banner::parse_banner(&text, src).ok();
    let dir = private_dir(&ctx.temp_dir)?;
    let mut by_scheme: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    for r in &refs {
        let uris = by_scheme.entry(r.scheme).or_default();
        if !uris.contains(&r.uri) {
            uris.push(r.uri);
        }
    }
    let mut values: BTreeMap<&str, String> = BTreeMap::new();
    for (scheme, uris) in by_scheme {
        let resolved = resolve(scheme, &uris, dir.path(), config, ctx, banner.as_ref())
            .map_err(|e| Error::Msg(format!("{}: {e}", src.display())))?;
        values.extend(uris.into_iter().zip(resolved));
    }
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for r in &refs {
        out.push_str(text.get(last..r.span.start).unwrap_or_default());
        out.push_str(values.get(r.uri).map_or("", String::as_str));
        last = r.span.end;
    }
    out.push_str(text.get(last..).unwrap_or_default());
    let path = dir
        .path()
        .join(format!("{}.decrypted.tfvars", crate::banner::stem(src)));
    // The directory was just made, private to tfp: writing into it does not fail short of a full disk.
    let _ = std::fs::write(&path, &out);
    Ok(Decrypted {
        path,
        contents: out.into_bytes(),
        dir: Some(dir),
    })
}

/// The secrets `uris` (all of `scheme`) name, in order, from the scheme's source.
fn resolve(
    scheme: &str,
    uris: &[&str],
    dir: &Path,
    config: &Config,
    ctx: &Ctx,
    banner: Option<&Banner>,
) -> Result<Vec<String>> {
    let no_env = BTreeMap::new();
    let settings = banner.and_then(|b| b.secrets_config.get(scheme));
    match scheme {
        "op" if settings.is_some_and(|s| !s.is_empty()) => Err(Error::Msg("op:// references take no settings".into())),
        "op" => one_password(uris, dir, &config.programs.op, ctx.search_path.clone()),
        "az-kv" => key_vault::key_vault(
            uris,
            key_vault::Access {
                settings,
                env: banner.map_or(&no_env, |b| &b.env),
                az: &config.programs.az,
                search_path: ctx.search_path.clone(),
                http: ctx.http.clone(),
            },
        ),
        other => Err(Error::Msg(format!(
            "no secret source for `{other}://` references (known: op, az-kv)"
        ))),
    }
}

/// 1Password: one `op inject` over a template holding just the references, each between markers that carry a
/// random nonce, so values with newlines or quotes come back intact.
fn one_password(uris: &[&str], dir: &Path, op: &str, search_path: Option<OsString>) -> Result<Vec<String>> {
    let digest = hex(&Sha256::digest(format!(
        "{}{:?}",
        dir.display(),
        std::time::SystemTime::now()
    )));
    let nonce = digest.get(..16).unwrap_or(&digest);
    let open = |i: usize| format!("@@tfp-{nonce}:{i}@@");
    let close = format!("@@tfp-{nonce}:end@@");
    let template = uris.iter().enumerate().fold(String::new(), |mut t, (i, uri)| {
        let _ = writeln!(t, "{}{{{{ {uri} }}}}{close}", open(i));
        t
    });
    let (input, output) = (dir.join("op.template"), dir.join("op.out"));

    let mut command = Command::new(op);
    command
        .arg("inject")
        .arg("--in-file")
        .arg(&input)
        .arg("--out-file")
        .arg(&output);
    command.envs(search_path.map(|path| ("PATH", path)));
    let status = std::fs::write(&input, template).and_then(|()| command.status()).map_err(|e| {
        Error::Msg(format!(
            "the 1Password CLI `{op}` could not run ({e}). Install it (https://developer.1password.com/docs/cli/get-started/) \
             or set its path as [tf_project.programs] op"
        ))
    })?;
    if !status.success() {
        return Err(Error::Msg(format!("`op inject` failed ({status})")));
    }
    let filled = std::fs::read_to_string(&output).map_err(io_error(&output));
    shred(&output);
    let filled = filled?;
    (0..uris.len())
        .map(|i| {
            let value = filled
                .split_once(&open(i))
                .and_then(|(_, rest)| rest.split_once(&close));
            value
                .map(|(v, _)| v.to_owned())
                .ok_or_else(|| Error::Msg("`op inject` returned something unexpected".into()))
        })
        .collect()
}

/// The schemes of the `{{ scheme://… }}` references in `text`, for `tfp self doctor`.
#[must_use]
pub fn schemes(text: &str) -> Vec<String> {
    references(text).into_iter().map(|r| r.scheme.to_owned()).collect()
}

#[cfg(test)]
mod tests {
    use pretty_assertions::assert_eq;

    use super::{Reference, materialize, references, schemes};
    use crate::testing::{Project, Streams};

    #[test]
    fn finds_scheme_references_only() {
        let text = "x = \"{{ git+ssh://h/r }}\"\ny = \"{{ a_b://z }}\"\na = \"{{ op://v/i/f }}\"\nb = \"{{op://v/j/f}}\"\nc = \"{{ not a ref }}\"\nd = \"{{ 9x://y }}\"\ne = \"{{ vault://k/p }}\"\nf = \"{{ open";
        let found: Vec<(&str, &str)> = references(text).iter().map(|r| (r.scheme, r.uri)).collect();
        assert_eq!(
            found,
            [
                ("git+ssh", "git+ssh://h/r"),
                ("op", "op://v/i/f"),
                ("op", "op://v/j/f"),
                ("vault", "vault://k/p")
            ]
        );
        assert_eq!(
            references("x = \"{{ op://a/b/c }}\"")[0],
            Reference {
                span: 5..21,
                uri: "op://a/b/c",
                scheme: "op"
            }
        );
        assert_eq!(schemes(text), ["git+ssh", "op", "op", "vault"]);
    }

    /// A project with a fake `op` in `bin/`: each `{{ op://v/i/f }}` becomes `secret(v/i/f)`; `v/multi/x` a value
    /// with a newline and quotes; any `fail` in the template exits 1; any `garble` writes junk.
    fn with_fake_op(extra: &str) -> Project {
        let p = Project::new(extra);
        let op = p.write(
            "bin/op",
            "#!/bin/sh\necho \"$@\" >> \"$0.log\"\ngrep -q fail \"$3\" && exit 1\n\
             grep -q garble \"$3\" && { echo junk > \"$5\"; exit 0; }\n\
             perl -pe 's/\\{\\{ op:\\/\\/v\\/multi\\/x \\}\\}/line1\\nline2 \"q\"/g; s/\\{\\{ op:\\/\\/(.+?) \\}\\}/secret($1)/g' \"$3\" > \"$5\"\n",
        );
        std::fs::set_permissions(&op, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        p
    }

    fn decrypt(p: &Project, tfvars: &str) -> (Result<String, String>, String) {
        let src = p.write("tfvars/dev.tfvars", tfvars);
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        let path = format!(
            "{}:{}",
            p.path("bin").display(),
            std::env::var("PATH").unwrap_or_default()
        );
        ctx.search_path = Some(path.into());
        let result = materialize(&p.config(), &mut ctx, &src).map(|d| {
            let text = std::fs::read_to_string(d.path()).unwrap();
            if d.path() != src {
                drop(d);
            }
            text
        });
        (result.map_err(|e| e.to_string()), s.err())
    }

    #[test]
    fn resolves_op_references_in_one_call() {
        let p = with_fake_op("");
        let tfvars = "a = \"{{ op://v/i/f }}\"\nb = \"{{ op://v/multi/x }}\"\nc = \"{{ op://v/i/f }}\"\n";
        let (text, err) = decrypt(&p, tfvars);
        assert_eq!(
            text.unwrap(),
            "a = \"secret(v/i/f)\"\nb = \"line1\nline2 \"q\"\"\nc = \"secret(v/i/f)\"\n"
        );
        assert_eq!(err, "");
        let calls = std::fs::read_to_string(p.path("bin/op.log")).unwrap();
        assert_eq!(calls.lines().count(), 1);
        assert!(calls.starts_with("inject --in-file "), "{calls}");
    }

    #[test]
    fn names_with_spaces_are_references() {
        let p = with_fake_op("");
        let tfvars = "client_id = \"{{ op://Revolutionise/Fuji non-prod/Deployment Client Id }}\"\n\
                      audience  = \"api://{{ op://Revolutionise/Fuji non-prod/Deployment Client Id }}\"\n\
                      tenant    = \"https://login.microsoftonline.com/{{op://Revolutionise/Fuji non-prod/Deployment Tenant Id}}/v2.0\"\n\
                      broken    = \"{{ op://a\nb }}\"\n";
        let found: Vec<&str> = references(tfvars).iter().map(|r| r.uri).collect();
        assert_eq!(
            found,
            [
                "op://Revolutionise/Fuji non-prod/Deployment Client Id",
                "op://Revolutionise/Fuji non-prod/Deployment Client Id",
                "op://Revolutionise/Fuji non-prod/Deployment Tenant Id",
            ]
        );
        assert_eq!(
            decrypt(&p, tfvars).0.unwrap(),
            "client_id = \"secret(Revolutionise/Fuji non-prod/Deployment Client Id)\"\n\
             audience  = \"api://secret(Revolutionise/Fuji non-prod/Deployment Client Id)\"\n\
             tenant    = \"https://login.microsoftonline.com/secret(Revolutionise/Fuji non-prod/Deployment Tenant Id)/v2.0\"\n\
             broken    = \"{{ op://a\nb }}\"\n"
        );
    }

    #[test]
    fn references_inside_multi_line_strings_are_resolved() {
        let p = with_fake_op("");
        let tfvars =
            "config = <<EOT\nblablabla\n\n{{ .Values.name }}\n\n{{op://v/i/f}}\n\nkey: {{ op://v/multi/x }} end\nEOT\n";
        let found: Vec<&str> = references(tfvars).iter().map(|r| r.uri).collect();
        assert_eq!(found, ["op://v/i/f", "op://v/multi/x"]);
        assert_eq!(
            decrypt(&p, tfvars).0.unwrap(),
            "config = <<EOT\nblablabla\n\n{{ .Values.name }}\n\nsecret(v/i/f)\n\nkey: line1\nline2 \"q\" end\nEOT\n"
        );
    }

    #[test]
    fn mixed_sources_in_one_file_with_banner_settings() {
        let p = with_fake_op("");
        let az = super::key_vault::tests::with_fake_az();
        std::fs::copy(az.path("bin/az"), p.path("bin/az")).unwrap();
        let vault = super::key_vault::tests::vault(&[("secrets/db", r#"{"value":"kv-secret"}"#)]);
        let banner = r#"# {"header":"terraform","project":"demo","env":{"ARM_TENANT_ID":"11111111-1111-1111-1111-111111111111"},"secrets_config":{"az-kv":{"subscription":"sub-x"}}}"#;
        let src = p.write(
            "tfvars/dev.tfvars",
            &format!("{banner}\na = \"{{{{ op://v/i/f }}}}\"\nb = \"{{{{ az-kv://kv/secrets/db }}}}\"\n"),
        );
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.search_path = Some(super::key_vault::tests::search_path(&p));
        ctx.http = Some(vault.clone());
        let d = materialize(&p.config(), &mut ctx, &src).unwrap();
        let text = String::from_utf8(d.contents().to_vec()).unwrap();
        assert!(text.ends_with("a = \"secret(v/i/f)\"\nb = \"kv-secret\"\n"), "{text}");
        let az_calls = std::fs::read_to_string(p.path("bin/az.log")).unwrap();
        assert!(
            az_calls.contains("--tenant 11111111-1111-1111-1111-111111111111") && az_calls.contains("sub-x"),
            "{az_calls}"
        );
        // Settings for `op` are refused: it takes none.
        p.write("tfvars/dev.tfvars", "# {\"header\":\"terraform\",\"project\":\"demo\",\"secrets_config\":{\"op\":{\"account\":\"x\"}}}\na = \"{{ op://v/i/f }}\"\n");
        let err = materialize(&p.config(), &mut ctx, &src).unwrap_err().to_string();
        assert!(err.ends_with("op:// references take no settings"), "{err}");
        // An empty settings table for `op` is fine.
        p.write("tfvars/dev.tfvars", "# {\"header\":\"terraform\",\"project\":\"demo\",\"secrets_config\":{\"op\":{}}}\na = \"{{ op://v/i/f }}\"\n");
        assert!(materialize(&p.config(), &mut ctx, &src).is_ok());
    }

    #[test]
    fn a_file_without_references_is_used_as_is() {
        let p = with_fake_op("");
        assert_eq!(decrypt(&p, "x = 1\n").0.unwrap(), "x = 1\n");
        assert!(!p.path("bin/op.log").exists());
    }

    #[test]
    fn decrypted_copies_are_shredded() {
        let p = with_fake_op("");
        let src = p.write("tfvars/dev.tfvars", "a = \"{{ op://v/i/f }}\"\n");
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.search_path = Some(format!("{}:{}", p.path("bin").display(), std::env::var("PATH").unwrap()).into());
        let d = materialize(&p.config(), &mut ctx, &src).unwrap();
        let (copy, dir) = (d.path().to_path_buf(), d.path().parent().unwrap().to_path_buf());
        assert!(copy.ends_with("dev.decrypted.tfvars"));
        assert!(!dir.join("op.out").exists());
        drop(d);
        assert!(!copy.exists() && !dir.exists());
    }

    #[test]
    fn failures() {
        let p = with_fake_op("");
        let err = |tfvars: &str| decrypt(&p, tfvars).0.unwrap_err();
        assert!(
            err("a = \"{{ vault://k }}\"\n").ends_with("no secret source for `vault://` references (known: op, az-kv)")
        );
        assert!(err("a = \"{{ op://v/fail/x }}\"\n").ends_with("`op inject` failed (exit status: 1)"));
        assert!(err("a = \"{{ op://v/garble/x }}\"\n").ends_with("`op inject` returned something unexpected"));
        // `op` that writes no output at all.
        p.write("bin/op", "#!/bin/sh\nexit 0\n");
        assert!(err("a = \"{{ op://v/i/f }}\"\n").contains("op.out"));
        // No `op` anywhere.
        std::fs::remove_file(p.path("bin/op")).unwrap();
        let src = p.write("tfvars/dev.tfvars", "a = \"{{ op://v/i/f }}\"\n");
        let mut s = Streams::default();
        let mut ctx = s.ctx(&p.root);
        ctx.search_path = Some(p.path("bin").into());
        let missing = materialize(&p.config(), &mut ctx, &src).unwrap_err().to_string();
        assert!(
            missing.contains("the 1Password CLI `op` could not run") && missing.contains("[tf_project.programs] op"),
            "{missing}"
        );
        ctx.temp_dir = p.path("nope");
        let no_tmp = materialize(&p.config(), &mut ctx, &src).unwrap_err().to_string();
        assert!(
            no_tmp.starts_with("cannot create a private directory for secrets: "),
            "{no_tmp}"
        );
        assert!(materialize(&p.config(), &mut ctx, &p.path("missing.tfvars")).is_err());
    }

    #[test]
    fn the_old_secrets_command_is_ignored_with_a_warning() {
        let p = with_fake_op(
            "[tf_project.secrets]\ncommand = [\"op\", \"inject\", \"--in-file\", \"{in}\", \"--out-file\", \"{out}\"]",
        );
        let (text, err) = decrypt(&p, "a = \"{{ op://v/i/f }}\"\n");
        assert_eq!(text.unwrap(), "a = \"secret(v/i/f)\"\n");
        assert!(
            err.starts_with("warning: [tf_project.secrets] command is no longer used"),
            "{err}"
        );
    }

    #[test]
    fn a_configured_op_path_is_used() {
        // The fake `op` is in the project's bin/, not on PATH: only the configured path finds it.
        for setting in [
            "[tf_project.programs]\nop = \"bin/op\"",
            "[tf_project.secrets]\ncommand = [\"bin/op\", \"inject\"]",
        ] {
            let p = with_fake_op(setting);
            let src = p.write("tfvars/dev.tfvars", "a = \"{{ op://v/i/f }}\"\n");
            let mut s = Streams::default();
            let mut ctx = s.ctx(&p.root);
            // tfp's own PATH, which lacks the project's bin/.
            ctx.search_path = None;
            let d = materialize(&p.config(), &mut ctx, &src).unwrap();
            assert_eq!(d.contents(), b"a = \"secret(v/i/f)\"\n", "{setting}");
        }
    }
}
