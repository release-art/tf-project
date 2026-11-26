//! Azure Key Vault: `az-kv://<vault>/<secret>[/<version>]`, read with Microsoft's Azure SDK for Rust and signed in
//! through the Azure CLI (`az login`).
//!
//! Settings (banner `secrets_config."az-kv"`):
//! - `tenant`: the tenant to sign in to; the banner's `ARM_TENANT_ID` otherwise.
//! - `subscription`: the subscription whose account to use; the banner's `ARM_SUBSCRIPTION_ID` otherwise.
//! - `vault_url`: the vault URL, `{vault}` standing for its name; `https://{vault}.vault.azure.net/` otherwise
//!   (`https://{vault}.vault.azure.cn/` and the like for sovereign clouds).

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::sync::Arc;

use azure_core::http::{ClientOptions, HttpClient, Transport};
use azure_identity::{AzureCliCredential, AzureCliCredentialOptions, Executor};
use azure_security_keyvault_secrets::models::SecretClientGetSecretOptions;
use azure_security_keyvault_secrets::{SecretClient, SecretClientOptions};

use crate::{Error, Result, io_error};

/// The settings this source takes.
const SETTINGS: [&str; 3] = ["tenant", "subscription", "vault_url"];

/// What the source needs besides the references.
pub(super) struct Access<'a> {
    /// Its banner settings.
    pub(super) settings: Option<&'a BTreeMap<String, String>>,
    /// The banner's environment, for the tenant and subscription defaults.
    pub(super) env: &'a BTreeMap<String, String>,
    /// The Azure CLI (`[tf_project.programs] az`): a name on `PATH`, or a path.
    pub(super) az: &'a str,
    /// `PATH` for the Azure CLI; tfp's own when none.
    pub(super) search_path: Option<OsString>,
    /// The HTTP client; the SDK's own when none.
    pub(super) http: Option<Arc<dyn HttpClient>>,
}

/// One `az-kv://` reference, taken apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Reference<'a> {
    uri: &'a str,
    vault: &'a str,
    name: &'a str,
    version: Option<&'a str>,
}

fn parse(uri: &str) -> Result<Reference<'_>> {
    let parts: Vec<&str> = uri.trim_start_matches("az-kv://").split('/').collect();
    match parts.as_slice() {
        [vault, name] | [vault, name, ""] if !vault.is_empty() && !name.is_empty() => Ok(Reference {
            uri,
            vault,
            name,
            version: None,
        }),
        [vault, name, version] if !vault.is_empty() && !name.is_empty() => Ok(Reference {
            uri,
            vault,
            name,
            version: Some(version),
        }),
        _ => Err(Error::Msg(format!(
            "`{uri}` is not az-kv://<vault>/<secret>[/<version>]"
        ))),
    }
}

/// An SDK error with its causes, which carry the useful part (the Azure CLI's "run `az login`", the service's
/// message).
fn describe(e: &azure_core::Error) -> String {
    let chain = std::iter::successors(Some(e as &dyn std::error::Error), |e| e.source());
    chain.map(ToString::to_string).collect::<Vec<_>>().join(": ")
}

/// The SDK runs plain `az` through a shell, so a configured `az` path gets its directory put first on the `PATH`.
fn az_search_path(az: &str, search_path: Option<OsString>) -> Option<OsString> {
    let Some(dir) = Path::new(az).parent().filter(|d| !d.as_os_str().is_empty()) else {
        return search_path;
    };
    let rest = search_path.or_else(|| std::env::var_os("PATH")).unwrap_or_default();
    std::env::join_paths(std::iter::once(dir.to_path_buf()).chain(std::env::split_paths(&rest))).ok()
}

/// Runs the Azure CLI for the credential, on the `PATH` tfp was given.
#[derive(Debug)]
struct OnPath(Option<OsString>);

#[async_trait::async_trait]
impl Executor for OnPath {
    async fn run(&self, program: &OsStr, args: &[&OsStr]) -> std::io::Result<std::process::Output> {
        std::process::Command::new(program)
            .args(args)
            .envs(self.0.clone().map(|p| ("PATH", p)))
            .output()
    }
}

/// The secrets `uris` name, in order. Each vault signs in once, on its first secret; the rest are read concurrently.
pub(super) fn key_vault(uris: &[&str], access: Access<'_>) -> Result<Vec<String>> {
    let empty = BTreeMap::new();
    let settings = access.settings.unwrap_or(&empty);
    if let Some(unknown) = settings.keys().find(|k| !SETTINGS.contains(&k.as_str())) {
        return Err(Error::Msg(format!(
            "unknown az-kv setting `{unknown}` (known: {})",
            SETTINGS.join(", ")
        )));
    }
    let refs: Vec<Reference<'_>> = uris.iter().map(|uri| parse(uri)).collect::<Result<_>>()?;
    let setting = |key: &str, env: &str| settings.get(key).or_else(|| access.env.get(env)).cloned();
    let credential = AzureCliCredential::new(Some(AzureCliCredentialOptions {
        tenant_id: setting("tenant", "ARM_TENANT_ID"),
        subscription: setting("subscription", "ARM_SUBSCRIPTION_ID"),
        executor: Some(Arc::new(OnPath(az_search_path(access.az, access.search_path)))),
    }))
    .map_err(|e| Error::Msg(format!("Azure CLI sign-in settings: {}", describe(&e))))?;
    let url = settings
        .get("vault_url")
        .map_or("https://{vault}.vault.azure.net/", String::as_str);
    // One client per vault, shared by its references.
    let mut clients: BTreeMap<&str, Arc<SecretClient>> = BTreeMap::new();
    let mut jobs: Vec<(Reference<'_>, Arc<SecretClient>)> = Vec::with_capacity(refs.len());
    for r in refs {
        let client = if let Some(client) = clients.get(r.vault) {
            client.clone()
        } else {
            let options = SecretClientOptions {
                client_options: ClientOptions {
                    transport: access.http.clone().map(Transport::new),
                    ..ClientOptions::default()
                },
                ..SecretClientOptions::default()
            };
            let client = SecretClient::new(&url.replace("{vault}", r.vault), credential.clone(), Some(options))
                .map_err(|e| Error::Msg(format!("{}: {}", r.uri, describe(&e))))?;
            clients.entry(r.vault).or_insert(Arc::new(client)).clone()
        };
        jobs.push((r, client));
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(io_error(Path::new("async runtime")))
        .and_then(|runtime| runtime.block_on(read_all(&jobs)))
}

/// Read every reference: each vault's first one alone (it signs in), then all the others at once.
async fn read_all(jobs: &[(Reference<'_>, Arc<SecretClient>)]) -> Result<Vec<String>> {
    let mut values: Vec<Option<String>> = Vec::with_capacity(jobs.len());
    let mut seen: Vec<&str> = Vec::new();
    for (r, client) in jobs {
        let first = !seen.contains(&r.vault);
        seen.push(r.vault);
        values.push(if first { Some(read(r, client).await?) } else { None });
    }
    let rest = jobs
        .iter()
        .zip(&values)
        .filter(|(_, v)| v.is_none())
        .map(|((r, client), _)| read(r, client));
    let mut rest = futures::future::try_join_all(rest).await?.into_iter();
    Ok(values
        .into_iter()
        .map(|v| v.or_else(|| rest.next()).unwrap_or_default())
        .collect())
}

async fn read(r: &Reference<'_>, client: &SecretClient) -> Result<String> {
    let options = SecretClientGetSecretOptions {
        secret_version: r.version.map(str::to_owned),
        ..Default::default()
    };
    let secret = client
        .get_secret(r.name, Some(options))
        .await
        .and_then(azure_core::http::Response::into_model)
        .map_err(|e| Error::Msg(format!("{}: {}", r.uri, describe(&e))))?;
    secret
        .value
        .ok_or_else(|| Error::Msg(format!("{}: the secret has no value", r.uri)))
}

#[cfg(test)]
pub(crate) mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    use azure_core::http::headers::{HeaderName, Headers};
    use azure_core::http::{AsyncRawResponse, HttpClient, Request, StatusCode};
    use pretty_assertions::assert_eq;

    use super::{Access, Reference, az_search_path, key_vault, parse};
    use crate::testing::Project;

    /// Key Vault, as far as tfp needs it: the first request of a client gets the auth challenge for the vault's
    /// domain; authorised ones get the secret named in the URL (`secrets/<name>[/<version>]`), 404 when it is unknown,
    /// and a secret named `novalue` has none.
    #[derive(Debug, Default)]
    pub(crate) struct FakeVault {
        pub(crate) secrets: BTreeMap<String, String>,
        pub(crate) requests: Mutex<Vec<String>>,
    }

    #[async_trait::async_trait]
    impl HttpClient for FakeVault {
        async fn execute_request(&self, request: &Request) -> azure_core::Result<AsyncRawResponse> {
            let url = request.url();
            let host = url.host_str().unwrap_or_default().to_owned();
            let authorised = request
                .headers()
                .get_optional_str(&HeaderName::from_static("authorization"))
                .is_some();
            let path = url.path().trim_end_matches('/');
            self.requests
                .lock()
                .unwrap()
                .push(format!("{} {host}{path}", if authorised { "auth" } else { "anon" }));
            let mut headers = Headers::new();
            if !authorised {
                let domain = host.split_once('.').map_or(host.as_str(), |(_, d)| d);
                let challenge =
                    format!(r#"Bearer authorization="https://login.example/t", resource="https://{domain}""#);
                headers.insert(HeaderName::from_static("www-authenticate"), challenge);
                return Ok(AsyncRawResponse::from_bytes(StatusCode::Unauthorized, headers, ""));
            }
            let path: Vec<&str> = path.trim_start_matches('/').split('/').collect();
            let key = path.get(1..).unwrap_or_default().join("/");
            headers.insert(HeaderName::from_static("content-type"), "application/json");
            Ok(match self.secrets.get(&key) {
                _ if key == "novalue" => AsyncRawResponse::from_bytes(StatusCode::Ok, headers, "{}"),
                Some(value) => {
                    let body = serde_json::json!({"value": value, "id": format!("https://{host}/secrets/{key}")});
                    AsyncRawResponse::from_bytes(StatusCode::Ok, headers, body.to_string())
                }
                None => AsyncRawResponse::from_bytes(
                    StatusCode::NotFound,
                    headers,
                    r#"{"error":{"code":"SecretNotFound","message":"no such secret"}}"#,
                ),
            })
        }
    }

    /// A project with a fake `az` in `bin/` that logs its arguments to `bin/az.log` and prints a token, or fails when
    /// a `FAIL` file sits next to it.
    pub(crate) fn with_fake_az() -> Project {
        let p = Project::new("");
        let az = p.write(
            "bin/az",
            "#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/az.log\"\n[ -f \"$(dirname \"$0\")/FAIL\" ] && { echo 'Please run az login' >&2; exit 1; }\n\
             echo '{\"accessToken\":\"tok\",\"expires_on\":4102444800,\"tokenType\":\"Bearer\"}'\n",
        );
        std::fs::set_permissions(&az, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        p
    }

    pub(crate) fn search_path(p: &Project) -> std::ffi::OsString {
        format!(
            "{}:{}",
            p.path("bin").display(),
            std::env::var("PATH").unwrap_or_default()
        )
        .into()
    }

    fn vault(secrets: &[(&str, &str)]) -> Arc<FakeVault> {
        Arc::new(FakeVault {
            secrets: secrets
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
            ..FakeVault::default()
        })
    }

    fn read(
        p: &Project,
        vault: &Arc<FakeVault>,
        uris: &[&str],
        settings: &[(&str, &str)],
        env: &[(&str, &str)],
    ) -> Result<Vec<String>, String> {
        let map = |kv: &[(&str, &str)]| kv.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect();
        let (settings, env): (BTreeMap<String, String>, BTreeMap<String, String>) = (map(settings), map(env));
        let http: Arc<dyn HttpClient> = vault.clone();
        let access = Access {
            az: "az",
            settings: Some(&settings),
            env: &env,
            search_path: Some(search_path(p)),
            http: Some(http),
        };
        key_vault(uris, access).map_err(|e| e.to_string())
    }

    #[test]
    fn a_configured_az_path_goes_first_on_path() {
        assert_eq!(az_search_path("az", Some("/x".into())), Some("/x".into()));
        assert_eq!(az_search_path("az", None), None);
        assert_eq!(
            az_search_path("/opt/az/bin/az", Some("/a:/b".into())),
            Some("/opt/az/bin:/a:/b".into())
        );
        let inherited = az_search_path("/opt/az/bin/az", None).unwrap();
        assert!(inherited.to_string_lossy().starts_with("/opt/az/bin:"), "{inherited:?}");
    }

    #[test]
    fn references_take_a_vault_a_name_and_maybe_a_version() {
        let r = |uri| parse(uri).unwrap();
        assert_eq!(
            r("az-kv://kv/name"),
            Reference {
                uri: "az-kv://kv/name",
                vault: "kv",
                name: "name",
                version: None
            }
        );
        assert_eq!(r("az-kv://kv/name/").version, None);
        assert_eq!(r("az-kv://kv/name/v1").version, Some("v1"));
        for bad in ["az-kv://kv", "az-kv:///name", "az-kv://kv//v", "az-kv://kv/a/b/c"] {
            assert!(parse(bad).unwrap_err().to_string().contains("is not az-kv://"), "{bad}");
        }
    }

    #[test]
    fn reads_secrets_signing_in_once_per_vault() {
        let p = with_fake_az();
        let v = vault(&[
            ("db", "s3cret"),
            ("api", "k\ney \"q\""),
            ("db/v1", "old"),
            ("other", "x"),
        ]);
        let uris = [
            "az-kv://one/db",
            "az-kv://one/api",
            "az-kv://one/db/v1",
            "az-kv://two/other",
        ];
        let env = [
            ("ARM_TENANT_ID", "11111111-1111-1111-1111-111111111111"),
            ("ARM_SUBSCRIPTION_ID", "sub-a"),
        ];
        assert_eq!(
            read(&p, &v, &uris, &[], &env).unwrap(),
            ["s3cret", "k\ney \"q\"", "old", "x"]
        );
        let az = std::fs::read_to_string(p.path("bin/az.log")).unwrap();
        assert_eq!(az.lines().count(), 2, "{az}");
        assert!(
            az.contains("--tenant 11111111-1111-1111-1111-111111111111") && az.contains("--subscription sub-a"),
            "{az}"
        );
        let requests = v.requests.lock().unwrap().clone();
        assert!(
            requests.contains(&"anon one.vault.azure.net/secrets/db".to_owned()),
            "{requests:?}"
        );
        assert!(
            requests.contains(&"auth two.vault.azure.net/secrets/other".to_owned()),
            "{requests:?}"
        );
    }

    #[test]
    fn settings_override_the_banner_env_and_pick_the_cloud() {
        let p = with_fake_az();
        let v = vault(&[("db", "s")]);
        let settings = [
            ("tenant", "22222222-2222-2222-2222-222222222222"),
            ("subscription", "sub-b"),
            ("vault_url", "https://{vault}.vault.azure.cn/"),
        ];
        let env = [("ARM_TENANT_ID", "11111111-1111-1111-1111-111111111111")];
        assert_eq!(read(&p, &v, &["az-kv://cn/db"], &settings, &env).unwrap(), ["s"]);
        let az = std::fs::read_to_string(p.path("bin/az.log")).unwrap();
        assert!(az.contains("--tenant 22222222-2222-2222-2222-222222222222") && az.contains("--subscription sub-b"));
        assert!(
            v.requests
                .lock()
                .unwrap()
                .contains(&"auth cn.vault.azure.cn/secrets/db".to_owned())
        );
        // No settings and no env: no tenant or subscription.
        std::fs::remove_file(p.path("bin/az.log")).unwrap();
        let http: Arc<dyn HttpClient> = v.clone();
        let empty = BTreeMap::new();
        let access = Access {
            az: "az",
            settings: None,
            env: &empty,
            search_path: Some(search_path(&p)),
            http: Some(http),
        };
        assert_eq!(key_vault(&["az-kv://kv/db"], access).unwrap(), ["s"]);
        assert!(
            !std::fs::read_to_string(p.path("bin/az.log"))
                .unwrap()
                .contains("--tenant")
        );
    }

    #[test]
    fn failures_name_the_reference() {
        let p = with_fake_az();
        let v = vault(&[("db", "s")]);
        let err = |uris: &[&str], settings: &[(&str, &str)]| read(&p, &v, uris, settings, &[]).unwrap_err();
        assert_eq!(
            err(&["az-kv://kv/db"], &[("region", "x")]),
            "unknown az-kv setting `region` (known: tenant, subscription, vault_url)"
        );
        assert!(err(&["az-kv://kv"], &[]).contains("is not az-kv://"));
        assert!(err(&["az-kv://kv/db"], &[("tenant", "not a tenant!")]).starts_with("Azure CLI sign-in settings: "));
        assert!(err(&["az-kv://kv/db"], &[("vault_url", "not a url {vault}")]).starts_with("az-kv://kv/db: "));
        assert!(err(&["az-kv://kv/missing"], &[]).starts_with("az-kv://kv/missing: "));
        assert!(err(&["az-kv://kv/db", "az-kv://kv/missing"], &[]).starts_with("az-kv://kv/missing: "));
        assert_eq!(
            err(&["az-kv://kv/novalue"], &[]),
            "az-kv://kv/novalue: the secret has no value"
        );
        p.write("bin/FAIL", "");
        let signed_out = err(&["az-kv://kv/db"], &[]);
        assert!(
            signed_out.starts_with("az-kv://kv/db: ") && signed_out.contains("az login"),
            "{signed_out}"
        );
    }
}
