//! Is the Azure CLI signed in where a slot expects? Checked before tfp changes an Azure slot; it never switches
//! accounts.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::config::Config;
use crate::{Ctx, Error, Result, say};

/// The parts of `az account show -o json` the check reads.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct AzAccount {
    /// The default subscription's id.
    pub id: String,
    /// Its name.
    pub name: String,
    /// The tenant az is signed in to.
    #[serde(rename = "tenantId")]
    pub tenant_id: String,
    /// Who is signed in.
    pub user: AzUser,
}

/// `user` in `az account show`.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct AzUser {
    /// The user or service principal.
    pub name: String,
}

/// What a slot's banner says about Azure, against the signed-in account.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Assessment {
    /// Why tfp must not go ahead.
    pub error: Option<String>,
    /// What the user should know.
    pub warning: Option<String>,
}

/// Whether a slot is an Azure one: its environment sets `ARM_*`, or its backend is azurerm.
#[must_use]
pub fn looks_azure(env: &BTreeMap<String, String>, backend_config: &BTreeMap<String, String>) -> bool {
    env.keys().any(|k| k.starts_with("ARM_")) || crate::lock::is_azurerm(backend_config)
}

/// The configured `az`, looked up on `search_path` (tfp's own `PATH` when none).
#[must_use]
pub fn find_az(config: &Config, search_path: Option<&std::ffi::OsStr>) -> Option<PathBuf> {
    let path = search_path.map(ToOwned::to_owned).or_else(|| std::env::var_os("PATH"));
    which::which_in(&config.programs.az, path, &config.project_root).ok()
}

/// `az account show`.
///
/// # Errors
/// az fails (no one is signed in) or prints something else than an account.
pub fn az_account(az: &Path, search_path: Option<OsString>) -> Result<AzAccount> {
    let output = std::process::Command::new(az)
        .args(["account", "show", "-o", "json"])
        .envs(search_path.map(|p| ("PATH", p)))
        .output()
        .map_err(|e| Error::Msg(format!("{}: {e}", az.display())))?;
    let failed = |detail: String| Error::Msg(format!("`az account show` failed ({detail}). Run `az login`."));
    if !output.status.success() {
        return Err(failed(String::from_utf8_lossy(&output.stderr).trim().to_owned()));
    }
    serde_json::from_slice(&output.stdout).map_err(|e| failed(e.to_string()))
}

/// A banner `ARM_TENANT_ID` other than az's tenant is an error; no `ARM_SUBSCRIPTION_ID` means Terraform falls back to
/// az's default subscription, which is worth a warning.
#[must_use]
pub fn assess(slot: &str, env: &BTreeMap<String, String>, account: &AzAccount) -> Assessment {
    let error = env
        .get("ARM_TENANT_ID")
        .filter(|t| **t != account.tenant_id)
        .map(|tenant| {
            format!(
                "slot {slot:?} expects tenant {tenant} but az is signed in to tenant {} ({}) - \
                 run `az login --tenant {tenant}`",
                account.tenant_id, account.user.name
            )
        });
    let warning = (!env.contains_key("ARM_SUBSCRIPTION_ID")).then(|| {
        format!(
            "slot {slot:?} sets no ARM_SUBSCRIPTION_ID; Terraform will use az's default subscription {} ({})",
            account.name, account.id
        )
    });
    Assessment { error, warning }
}

/// Before changing an Azure slot: refuse when az is signed in to another tenant, warn when no subscription is pinned.
/// Nothing is checked for other slots, with `--dry-run`, or when `az` is not installed.
///
/// # Errors
/// az is signed in to another tenant, or no one is signed in.
pub fn check_azure_identity(
    config: &Config,
    ctx: &mut Ctx,
    slot: &str,
    env: &BTreeMap<String, String>,
    backend_config: &BTreeMap<String, String>,
) -> Result<()> {
    if ctx.terraform.dry_run || !looks_azure(env, backend_config) {
        return Ok(());
    }
    let Some(az) = find_az(config, ctx.search_path.as_deref()) else {
        return Ok(());
    };
    let found = assess(slot, env, &az_account(&az, ctx.search_path.clone())?);
    if let Some(warning) = found.warning {
        say!(ctx.err, "warning: {warning}");
    }
    found.error.map_or(Ok(()), |e| Err(Error::Msg(e)))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use pretty_assertions::assert_eq;

    use super::{Assessment, AzAccount, AzUser, assess};

    #[test]
    fn tenant_and_subscription() {
        let account = AzAccount {
            id: "sub-1".into(),
            name: "Default".into(),
            tenant_id: "t-1".into(),
            user: AzUser { name: "me@x".into() },
        };
        let env = |pairs: &[(&str, &str)]| -> BTreeMap<String, String> {
            pairs.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect()
        };
        assert_eq!(
            assess(
                "prod",
                &env(&[("ARM_TENANT_ID", "t-1"), ("ARM_SUBSCRIPTION_ID", "s")]),
                &account
            ),
            Assessment::default()
        );
        let found = assess("prod", &env(&[("ARM_TENANT_ID", "t-2")]), &account);
        assert_eq!(
            found.error.unwrap(),
            "slot \"prod\" expects tenant t-2 but az is signed in to tenant t-1 (me@x) - run `az login --tenant t-2`"
        );
        assert_eq!(
            found.warning.unwrap(),
            "slot \"prod\" sets no ARM_SUBSCRIPTION_ID; Terraform will use az's default subscription Default (sub-1)"
        );
    }
}
