//! The remote-state lock, read and broken through the cloud's own CLI: `az` for azurerm, `aws` for S3 + DynamoDB.
//!
//! The polite way to release a lock, `terraform force-unlock <ID>`, lives in `self_commands`; it only asks a
//! provider for the lock ID.

use std::process::Command;

use base64::Engine;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::models::MyState;
use crate::{Error, Result};

/// Terraform's lock metadata, the fields tfp shows.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
pub struct LockInfo {
    /// The lock ID `force-unlock` takes.
    #[serde(rename = "ID")]
    pub id: Option<String>,
    /// Who holds it.
    #[serde(rename = "Who")]
    pub who: Option<String>,
    /// What they are doing.
    #[serde(rename = "Operation")]
    pub operation: Option<String>,
    /// Since when.
    #[serde(rename = "Created")]
    pub created: Option<String>,
}

impl LockInfo {
    fn parse(json: &[u8]) -> Self {
        serde_json::from_slice(json).unwrap_or_default()
    }
}

/// The lock as the backend reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockStatus {
    /// `azurerm` or `s3`.
    pub backend: &'static str,
    /// Whether it is held.
    pub locked: bool,
    /// A backend-specific one-line summary.
    pub detail: String,
    /// Terraform's metadata, when the backend has it.
    pub info: LockInfo,
}

/// The backends tfp knows how to inspect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Backend {
    /// azurerm: a blob lease, with the lock in the blob's metadata.
    Azure,
    /// S3 with DynamoDB locking: an item in the table.
    S3,
}

/// A slot's lock, and the CLI that reaches it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockProvider {
    /// Which backend.
    pub backend: Backend,
    /// The CLI: `az` or `aws`, looked up on `PATH`.
    pub program: String,
    /// The `PATH` to look it up on; tfp's own when none.
    pub search_path: Option<std::ffi::OsString>,
    state: MyState,
}

fn strs(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

/// Whether `backend_config` is for the azurerm backend.
#[must_use]
pub fn is_azurerm(backend_config: &std::collections::BTreeMap<String, String>) -> bool {
    backend_config.contains_key("storage_account_name") && backend_config.contains_key("container_name")
}

/// The provider for the slot's backend.
///
/// # Errors
/// The saved backend configuration is not azurerm, nor S3 with a DynamoDB table.
pub fn select_provider(state: &MyState) -> Result<LockProvider> {
    let bc = &state.backend_config;
    let (backend, program) = if is_azurerm(bc) {
        (Backend::Azure, "az")
    } else if bc.contains_key("dynamodb_table") && bc.contains_key("bucket") {
        (Backend::S3, "aws")
    } else if bc.contains_key("bucket") {
        return Err(Error::Msg(
            "`tfp self lock` handles S3 locks in DynamoDB only, and this backend has no `dynamodb_table`. \
             For an S3 lock file, use `tfp tf force-unlock <lock id>`."
                .into(),
        ));
    } else {
        return Err(Error::Msg(format!(
            "`tfp self lock` handles azurerm and s3 (with `dynamodb_table`) backends; this slot's backend settings \
             ({:?}) are neither. Use `tfp tf force-unlock <lock id>`.",
            bc.keys().collect::<Vec<_>>()
        )));
    };
    Ok(LockProvider {
        backend,
        program: program.into(),
        search_path: None,
        state: state.clone(),
    })
}

impl LockProvider {
    /// `azurerm` or `s3`.
    #[must_use]
    pub fn name(&self) -> &'static str {
        match self.backend {
            Backend::Azure => "azurerm",
            Backend::S3 => "s3",
        }
    }

    /// The `backend_config` values `keys` names, or an error naming the missing ones.
    fn required<const N: usize>(&self, keys: [&str; N], hint: &str) -> Result<[String; N]> {
        let values = keys.map(|k| self.state.backend_config.get(k).cloned().unwrap_or_default());
        let missing: Vec<&str> = keys
            .iter()
            .zip(&values)
            .filter(|(_, v)| v.is_empty())
            .map(|(k, _)| *k)
            .collect();
        if missing.is_empty() {
            return Ok(values);
        }
        Err(Error::Msg(format!(
            "{} backend metadata missing from saved state: {}.{hint}",
            self.label(),
            missing.join(", ")
        )))
    }

    fn label(&self) -> &'static str {
        match self.backend {
            Backend::Azure => "Azure",
            Backend::S3 => "S3",
        }
    }

    fn blob_args(&self) -> Result<Vec<String>> {
        let [account, container, key] = self.required(["storage_account_name", "container_name", "key"], "")?;
        let mut args = vec![
            "--account-name".into(),
            account,
            "--container-name".into(),
            container,
            "--blob-name".into(),
            key,
        ];
        if let Some(rg) = self.state.backend_config.get("resource_group_name") {
            args.extend(["--resource-group".into(), rg.clone()]);
        }
        if let Some(sub) = self.state.environ.get("ARM_SUBSCRIPTION_ID") {
            args.extend(["--subscription".into(), sub.clone()]);
        }
        Ok(args)
    }

    /// `--table-name <t> --key <LockID>`, then `--region <r>` when the region is known.
    fn item_args(&self) -> Result<Vec<String>> {
        let hint = " Note: `tfp self lock` requires the DynamoDB-locking variant of the S3 backend.";
        let [table, bucket, key] = self.required(["dynamodb_table", "bucket", "key"], hint)?;
        let lock_id = json!({"LockID": {"S": format!("{bucket}/{key}")}}).to_string();
        let env = &self.state.environ;
        let region = self
            .state
            .backend_config
            .get("region")
            .or_else(|| env.get("AWS_REGION"));
        let region = region.or_else(|| env.get("AWS_DEFAULT_REGION"));
        let mut args = vec!["--table-name".into(), table, "--key".into(), lock_id];
        args.extend(region.map(|r| ["--region".to_owned(), r.clone()]).into_iter().flatten());
        Ok(args)
    }

    /// The lock as the backend reports it.
    ///
    /// # Errors
    /// The backend metadata is incomplete, or the CLI fails.
    pub fn status(&self) -> Result<LockStatus> {
        let text = |data: &Value, pointer: &str| data.pointer(pointer).and_then(Value::as_str).map(str::to_owned);
        let (locked, detail, info) = match self.backend {
            Backend::Azure => {
                let data = self.run(
                    [
                        strs(&["storage", "blob", "show"]),
                        self.blob_args()?,
                        strs(&["-o", "json"]),
                    ]
                    .concat(),
                )?;
                let lock =
                    text(&data, "/metadata/terraformlockid").or_else(|| text(&data, "/metadata/Terraformlockid"));
                let info = lock
                    .and_then(|b64| base64::engine::general_purpose::STANDARD.decode(b64).ok())
                    .map_or_else(LockInfo::default, |json| LockInfo::parse(&json));
                let state = text(&data, "/properties/lease/state").unwrap_or_else(|| "unknown".into());
                let duration = text(&data, "/properties/lease/duration")
                    .map_or_else(String::new, |d| format!(" lease_duration={d}"));
                let locked = text(&data, "/properties/lease/status").is_some_and(|s| s == "locked");
                (locked, format!("lease_state={state}{duration}"), info)
            }
            Backend::S3 => {
                let data = self.run(
                    [
                        strs(&["dynamodb", "get-item"]),
                        self.item_args()?,
                        strs(&["--output", "json"]),
                    ]
                    .concat(),
                )?;
                let info =
                    text(&data, "/Item/Info/S").map_or_else(LockInfo::default, |s| LockInfo::parse(s.as_bytes()));
                let locked = info.id.is_some();
                (locked, if locked { "lock_present" } else { "no_lock" }.to_owned(), info)
            }
        };
        Ok(LockStatus {
            backend: self.name(),
            locked,
            detail,
            info,
        })
    }

    /// Break the lock at the backend: the blob lease, or the DynamoDB item.
    ///
    /// # Errors
    /// The backend metadata is incomplete, or the CLI fails.
    pub fn break_lease(&self) -> Result<()> {
        match self.backend {
            Backend::Azure => self.run([strs(&["storage", "blob", "lease", "break"]), self.blob_args()?].concat()),
            Backend::S3 => self.run([strs(&["dynamodb", "delete-item"]), self.item_args()?].concat()),
        }
        .map(drop)
    }

    /// Run the CLI and parse what it prints as JSON (nothing reads as `{}`).
    fn run(&self, args: Vec<String>) -> Result<Value> {
        let mut command = Command::new(&self.program);
        command.args(args);
        if let Some(path) = &self.search_path {
            command.env("PATH", path);
        }
        let output = command.output().map_err(|e| {
            let install = match self.backend {
                Backend::Azure => "https://learn.microsoft.com/cli/azure/install-azure-cli",
                Backend::S3 => "https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html",
            };
            Error::Msg(format!("`{}` could not run ({e}). Install: {install}", self.program))
        })?;
        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let detail = if stderr.trim().is_empty() {
                output.status.to_string()
            } else {
                stderr.trim().to_owned()
            };
            return Err(Error::Msg(format!("{} failed: {detail}", self.program)));
        }
        Ok(serde_json::from_slice(&output.stdout).unwrap_or(json!({})))
    }
}

#[cfg(test)]
mod tests {
    use base64::Engine;
    use pretty_assertions::assert_eq;

    use super::{Backend, LockInfo, LockProvider, select_provider};
    use crate::models::MyState;
    use crate::testing::Project;

    pub(crate) fn state(backend: &[(&str, &str)], environ: &[(&str, &str)]) -> MyState {
        let map = |kv: &[(&str, &str)]| kv.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect();
        MyState {
            slot: "dev".to_owned(),
            project: "demo".to_owned(),
            tfvars: "/t/dev.tfvars".into(),
            source_root: "/t/src".into(),
            tfplan_location: "/t/plan".into(),
            environ: map(environ),
            backend_config: map(backend),
        }
    }

    /// A fake CLI that logs its arguments to `cli.log`, prints `stdout` and exits with `code`.
    fn fake(p: &Project, provider: &mut LockProvider, stdout: &str, code: i32) {
        use std::os::unix::fs::PermissionsExt;
        let out = p.write("cli.out", stdout);
        let script = format!(
            "#!/bin/sh\necho \"$@\" >> '{}'\ncat '{}'\necho oops >&2\nexit {code}\n",
            p.path("cli.log").display(),
            out.display()
        );
        let path = p.write("bin/cli", &script);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        provider.program = path.display().to_string();
    }

    fn calls(p: &Project) -> Vec<String> {
        std::fs::read_to_string(p.path("cli.log"))
            .unwrap()
            .lines()
            .map(str::to_owned)
            .collect()
    }

    const AZURE: [(&str, &str); 4] = [
        ("storage_account_name", "acct"),
        ("container_name", "c"),
        ("key", "k"),
        ("resource_group_name", "rg"),
    ];

    #[test]
    fn azure_lease_and_metadata() {
        let p = Project::new("");
        let mut provider = select_provider(&state(&AZURE, &[("ARM_SUBSCRIPTION_ID", "sub")])).unwrap();
        assert_eq!(
            (provider.backend, provider.program.as_str(), provider.name()),
            (Backend::Azure, "az", "azurerm")
        );
        let lock = base64::engine::general_purpose::STANDARD.encode(r#"{"ID":"abc","Who":"me"}"#);
        let blob = format!(
            r#"{{"properties":{{"lease":{{"status":"locked","state":"leased","duration":"infinite"}}}},"metadata":{{"Terraformlockid":"{lock}"}}}}"#
        );
        fake(&p, &mut provider, &blob, 0);
        let status = provider.status().unwrap();
        assert!(status.locked);
        assert_eq!(status.detail, "lease_state=leased lease_duration=infinite");
        assert_eq!(
            status.info,
            LockInfo {
                id: Some("abc".into()),
                who: Some("me".into()),
                ..LockInfo::default()
            }
        );
        provider.break_lease().unwrap();
        assert_eq!(
            calls(&p),
            [
                "storage blob show --account-name acct --container-name c --blob-name k --resource-group rg --subscription sub -o json",
                "storage blob lease break --account-name acct --container-name c --blob-name k --resource-group rg --subscription sub",
            ]
        );
        // No output, or metadata that is not base64 JSON: unlocked, no info.
        for out in ["", r#"{"metadata":{"terraformlockid":"!!"}}"#] {
            fake(&p, &mut provider, out, 0);
            let status = provider.status().unwrap();
            assert_eq!(
                (status.locked, status.detail.as_str(), status.info),
                (false, "lease_state=unknown", LockInfo::default())
            );
        }
    }

    #[test]
    fn s3_item() {
        let p = Project::new("");
        let backend = [("dynamodb_table", "t"), ("bucket", "b"), ("key", "k")];
        let mut provider = select_provider(&state(&backend, &[("AWS_DEFAULT_REGION", "eu-west-1")])).unwrap();
        assert_eq!((provider.backend, provider.name()), (Backend::S3, "s3"));
        fake(
            &p,
            &mut provider,
            r#"{"Item":{"Info":{"S":"{\"ID\":\"x\",\"Operation\":\"apply\"}"}}}"#,
            0,
        );
        let status = provider.status().unwrap();
        assert_eq!((status.locked, status.detail.as_str()), (true, "lock_present"));
        assert_eq!(status.info.operation.as_deref(), Some("apply"));
        provider.break_lease().unwrap();
        let key = r#"{"LockID":{"S":"b/k"}}"#;
        assert_eq!(
            calls(&p),
            [
                format!("dynamodb get-item --table-name t --key {key} --region eu-west-1 --output json"),
                format!("dynamodb delete-item --table-name t --key {key} --region eu-west-1"),
            ]
        );
        let mut no_region = select_provider(&state(&backend, &[])).unwrap();
        fake(&p, &mut no_region, "{}", 0);
        assert_eq!(no_region.status().unwrap().detail, "no_lock");
        assert!(calls(&p).last().unwrap().ends_with("--output json"));
    }

    #[test]
    fn unsupported_and_incomplete_backends() {
        let err = |b: &[(&str, &str)]| select_provider(&state(b, &[])).unwrap_err().to_string();
        assert!(err(&[("bucket", "b")]).starts_with("`tfp self lock` handles S3 locks in DynamoDB only"));
        assert!(err(&[("path", "x")]).contains("([\"path\"]) are neither"));
        let azure = select_provider(&state(&[("storage_account_name", "a"), ("container_name", "")], &[])).unwrap();
        assert_eq!(
            azure.status().unwrap_err().to_string(),
            "Azure backend metadata missing from saved state: container_name, key."
        );
        let s3 = select_provider(&state(&[("dynamodb_table", "t"), ("bucket", "b")], &[])).unwrap();
        assert!(s3.status().is_err());
        assert!(
            s3.break_lease()
                .unwrap_err()
                .to_string()
                .starts_with("S3 backend metadata missing from saved state: key. Note:")
        );
    }

    #[test]
    fn cli_failures() {
        let p = Project::new("");
        let mut provider = select_provider(&state(&AZURE, &[])).unwrap();
        provider.program = "/no/such/az".into();
        assert!(
            provider
                .status()
                .unwrap_err()
                .to_string()
                .contains("Install: https://learn.microsoft.com")
        );
        let mut s3 = select_provider(&state(&[("dynamodb_table", "t"), ("bucket", "b"), ("key", "k")], &[])).unwrap();
        s3.program = "/no/such/aws".into();
        assert!(
            s3.status()
                .unwrap_err()
                .to_string()
                .contains("Install: https://docs.aws.amazon.com")
        );
        fake(&p, &mut provider, "", 3);
        assert!(provider.status().unwrap_err().to_string().ends_with("failed: oops"));
        let quiet = p.write("bin/quiet", "#!/bin/sh\nexit 4\n");
        std::fs::set_permissions(&quiet, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        provider.program = quiet.display().to_string();
        assert!(
            provider
                .break_lease()
                .unwrap_err()
                .to_string()
                .contains("failed: exit status: 4")
        );
    }
}
