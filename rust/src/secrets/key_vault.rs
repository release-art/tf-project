//! Azure Key Vault: `az-kv://<vault>/<secrets|keys|certificates>/<name>[/<version>]`, read with Microsoft's Azure
//! SDK for Rust and signed in through the Azure CLI (`az login`).
//!
//! What each kind gives:
//! - `secrets`: the secret's value.
//! - `keys`: the key's public part, as the JSON Web Key Key Vault returns. Private key material never leaves Key Vault.
//! - `certificates`: the public certificate, as PEM. The certificate with its private key is the certificate's
//!   backing secret: `secrets/<certificate name>`.
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

use azure_core::credentials::{AccessToken, TokenCredential, TokenRequestOptions};
use azure_core::http::{ClientOptions, HttpClient, Response, Transport};
use azure_identity::{AzureCliCredential, AzureCliCredentialOptions, Executor};
use azure_security_keyvault_certificates::models::CertificateClientGetCertificateOptions;
use azure_security_keyvault_certificates::{CertificateClient, CertificateClientOptions};
use azure_security_keyvault_keys::models::KeyClientGetKeyOptions;
use azure_security_keyvault_keys::{KeyClient, KeyClientOptions};
use azure_security_keyvault_secrets::models::SecretClientGetSecretOptions;
use azure_security_keyvault_secrets::{SecretClient, SecretClientOptions};
use base64::Engine;

use crate::{Error, Result, io_error};

/// The settings this source takes.
const SETTINGS: [&str; 3] = ["tenant", "subscription", "vault_url"];

/// What the source needs besides the references.
pub(super) struct Access<'a> {
    /// The Azure CLI (`[tf_project.programs] az`): a name on `PATH`, or a path.
    pub(super) az: &'a str,
    /// Its banner settings.
    pub(super) settings: Option<&'a BTreeMap<String, String>>,
    /// The banner's environment, for the tenant and subscription defaults.
    pub(super) env: &'a BTreeMap<String, String>,
    /// `PATH` for the Azure CLI; tfp's own when none.
    pub(super) search_path: Option<OsString>,
    /// The HTTP client; the SDK's own when none.
    pub(super) http: Option<Arc<dyn HttpClient>>,
}

/// The three kinds of Key Vault object, by their URL path segment.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Kind {
    Secret,
    Key,
    Certificate,
}

/// One `az-kv://` reference, taken apart.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Reference<'a> {
    uri: &'a str,
    vault: &'a str,
    kind: Kind,
    name: &'a str,
    version: Option<&'a str>,
}

fn parse(uri: &str) -> Result<Reference<'_>> {
    let parts: Vec<&str> = uri.trim_start_matches("az-kv://").split('/').collect();
    let (vault, kind, name, version) = match parts.as_slice() {
        [vault, kind, name] | [vault, kind, name, ""] => (*vault, *kind, *name, None),
        [vault, kind, name, version] => (*vault, *kind, *name, Some(*version)),
        _ => ("", "", "", None),
    };
    let kind = match kind {
        "secrets" => Kind::Secret,
        "keys" => Kind::Key,
        "certificates" => Kind::Certificate,
        _ => return Err(not_a_reference(uri)),
    };
    if vault.is_empty() || name.is_empty() {
        return Err(not_a_reference(uri));
    }
    Ok(Reference {
        uri,
        vault,
        kind,
        name,
        version,
    })
}

fn not_a_reference(uri: &str) -> Error {
    Error::Msg(format!(
        "`{uri}` is not az-kv://<vault>/<secrets|keys|certificates>/<name>[/<version>]"
    ))
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

/// A credential that asks the Azure CLI once per scope and hands the same token to every client: one `az` call for
/// all a run's reads, however many vaults and kinds. Concurrent first requests wait for that one call.
// ponytail: cached for the run only, which is far shorter than a token's life; no refresh.
#[derive(Debug)]
struct SignInOnce {
    inner: Arc<dyn TokenCredential>,
    tokens: futures::lock::Mutex<BTreeMap<Vec<String>, AccessToken>>,
}

#[async_trait::async_trait]
impl TokenCredential for SignInOnce {
    async fn get_token(
        &self,
        scopes: &[&str],
        options: Option<TokenRequestOptions<'_>>,
    ) -> azure_core::Result<AccessToken> {
        let mut tokens = self.tokens.lock().await;
        let key: Vec<String> = scopes.iter().map(|s| (*s).to_owned()).collect();
        if let Some(token) = tokens.get(&key) {
            return Ok(token.clone());
        }
        let token = self.inner.get_token(scopes, options).await?;
        tokens.insert(key, token.clone());
        Ok(token)
    }
}

/// A Key Vault client for one kind of object.
enum Client {
    Secret(SecretClient),
    Key(KeyClient),
    Certificate(CertificateClient),
}

fn client(
    kind: Kind,
    url: &str,
    credential: Arc<dyn TokenCredential>,
    http: Option<&Arc<dyn HttpClient>>,
) -> azure_core::Result<Client> {
    let client_options = ClientOptions {
        transport: http.cloned().map(Transport::new),
        ..ClientOptions::default()
    };
    Ok(match kind {
        Kind::Secret => Client::Secret(SecretClient::new(
            url,
            credential,
            Some(SecretClientOptions {
                client_options,
                ..SecretClientOptions::default()
            }),
        )?),
        Kind::Key => Client::Key(KeyClient::new(
            url,
            credential,
            Some(KeyClientOptions {
                client_options,
                ..KeyClientOptions::default()
            }),
        )?),
        Kind::Certificate => Client::Certificate(CertificateClient::new(
            url,
            credential,
            Some(CertificateClientOptions {
                client_options,
                ..CertificateClientOptions::default()
            }),
        )?),
    })
}

/// The values `uris` name, in order, all read concurrently.
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
    let cli = AzureCliCredential::new(Some(AzureCliCredentialOptions {
        tenant_id: setting("tenant", "ARM_TENANT_ID"),
        subscription: setting("subscription", "ARM_SUBSCRIPTION_ID"),
        executor: Some(Arc::new(OnPath(az_search_path(access.az, access.search_path)))),
    }))
    .map_err(|e| Error::Msg(format!("Azure CLI sign-in settings: {}", describe(&e))))?;
    let credential: Arc<dyn TokenCredential> = Arc::new(SignInOnce {
        inner: cli,
        tokens: futures::lock::Mutex::new(BTreeMap::new()),
    });
    let url = settings
        .get("vault_url")
        .map_or("https://{vault}.vault.azure.net/", String::as_str);
    // One client per vault and kind, shared by its references.
    let mut clients: BTreeMap<(&str, Kind), Arc<Client>> = BTreeMap::new();
    let mut jobs: Vec<(Reference<'_>, Arc<Client>)> = Vec::with_capacity(refs.len());
    for r in refs {
        let client = if let Some(client) = clients.get(&(r.vault, r.kind)) {
            client.clone()
        } else {
            let made = client(
                r.kind,
                &url.replace("{vault}", r.vault),
                credential.clone(),
                access.http.as_ref(),
            )
            .map_err(|e| Error::Msg(format!("{}: {}", r.uri, describe(&e))))?;
            clients.entry((r.vault, r.kind)).or_insert(Arc::new(made)).clone()
        };
        jobs.push((r, client));
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(io_error(Path::new("async runtime")))
        .and_then(|runtime| {
            runtime.block_on(futures::future::try_join_all(
                jobs.iter().map(|(r, client)| read(r, client)),
            ))
        })
}

async fn read(r: &Reference<'_>, client: &Client) -> Result<String> {
    let version = r.version.map(str::to_owned);
    let value = match client {
        Client::Secret(c) => {
            let options = SecretClientGetSecretOptions {
                secret_version: version,
                ..Default::default()
            };
            c.get_secret(r.name, Some(options))
                .await
                .and_then(Response::into_model)
                .map(|s| s.value)
        }
        Client::Key(c) => {
            let options = KeyClientGetKeyOptions {
                key_version: version,
                ..Default::default()
            };
            let key = c.get_key(r.name, Some(options)).await.and_then(Response::into_model);
            key.map(|k| k.key.and_then(|jwk| serde_json::to_string(&jwk).ok()))
        }
        Client::Certificate(c) => {
            let options = CertificateClientGetCertificateOptions {
                certificate_version: version,
                ..Default::default()
            };
            let certificate = c
                .get_certificate(r.name, Some(options))
                .await
                .and_then(Response::into_model);
            certificate.map(|c| c.cer.as_deref().map(pem))
        }
    }
    .map_err(|e| Error::Msg(format!("{}: {}", r.uri, describe(&e))))?;
    value.ok_or_else(|| Error::Msg(format!("{}: Key Vault returned no value", r.uri)))
}

/// A DER certificate as PEM.
fn pem(der: &[u8]) -> String {
    let b64 = base64::engine::general_purpose::STANDARD.encode(der);
    let lines: Vec<&str> = b64
        .as_bytes()
        .chunks(64)
        .filter_map(|line| std::str::from_utf8(line).ok())
        .collect();
    format!(
        "-----BEGIN CERTIFICATE-----\n{}\n-----END CERTIFICATE-----",
        lines.join("\n")
    )
}

#[cfg(test)]
pub(crate) mod tests {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    use azure_core::http::headers::{HeaderName, Headers};
    use azure_core::http::{AsyncRawResponse, HttpClient, Request, StatusCode};
    use base64::Engine;
    use pretty_assertions::assert_eq;

    use super::{Access, Kind, Reference, az_search_path, key_vault, parse, pem};
    use crate::testing::Project;

    /// Key Vault, as far as tfp needs it: a request without a token gets the auth challenge for the vault's domain;
    /// one with a token gets the JSON body stored under its path (`secrets/<name>[/<version>]`, `keys/…`,
    /// `certificates/…`), or 404.
    #[derive(Debug, Default)]
    pub(crate) struct FakeVault {
        pub(crate) bodies: BTreeMap<String, String>,
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
            let path = url.path().trim_matches('/');
            self.requests
                .lock()
                .unwrap()
                .push(format!("{} {host}/{path}", if authorised { "auth" } else { "anon" }));
            let mut headers = Headers::new();
            if !authorised {
                let domain = host.split_once('.').map_or(host.as_str(), |(_, d)| d);
                let challenge =
                    format!(r#"Bearer authorization="https://login.example/t", resource="https://{domain}""#);
                headers.insert(HeaderName::from_static("www-authenticate"), challenge);
                return Ok(AsyncRawResponse::from_bytes(StatusCode::Unauthorized, headers, ""));
            }
            headers.insert(HeaderName::from_static("content-type"), "application/json");
            Ok(match self.bodies.get(path) {
                Some(body) => AsyncRawResponse::from_bytes(StatusCode::Ok, headers, body.clone()),
                None => AsyncRawResponse::from_bytes(
                    StatusCode::NotFound,
                    headers,
                    r#"{"error":{"code":"NotFound","message":"no such object"}}"#,
                ),
            })
        }
    }

    /// A vault serving `bodies`: `(path, JSON body)`.
    pub(crate) fn vault(bodies: &[(&str, &str)]) -> Arc<FakeVault> {
        Arc::new(FakeVault {
            bodies: bodies.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect(),
            ..FakeVault::default()
        })
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

    fn az_calls(p: &Project) -> String {
        std::fs::read_to_string(p.path("bin/az.log")).unwrap_or_default()
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
    fn references_name_a_vault_a_kind_a_name_and_maybe_a_version() {
        let r = |uri| parse(uri).unwrap();
        assert_eq!(
            r("az-kv://kv/secrets/db"),
            Reference {
                uri: "az-kv://kv/secrets/db",
                vault: "kv",
                kind: Kind::Secret,
                name: "db",
                version: None
            }
        );
        assert_eq!(r("az-kv://kv/keys/sign/").kind, Kind::Key);
        assert_eq!(r("az-kv://kv/certificates/tls/v1").version, Some("v1"));
        for bad in [
            "az-kv://kv/db",
            "az-kv://kv/blobs/x",
            "az-kv:///secrets/x",
            "az-kv://kv/secrets/",
            "az-kv://kv/secrets/a/b/c",
        ] {
            let err = parse(bad).unwrap_err().to_string();
            assert!(
                err.contains("is not az-kv://<vault>/<secrets|keys|certificates>"),
                "{bad}"
            );
        }
    }

    #[test]
    fn reads_all_three_kinds_signing_in_once() {
        let p = with_fake_az();
        let der = [0x30_u8; 100];
        let cer = base64::engine::general_purpose::STANDARD.encode(der);
        let v = vault(&[
            ("secrets/db", r#"{"value":"s3cret"}"#),
            ("secrets/db/v1", r#"{"value":"old"}"#),
            ("secrets/api", r#"{"value":"k\ney \"q\""}"#),
            ("keys/sign", r#"{"key":{"kty":"RSA","n":"AQAB","e":"AQAB"}}"#),
            ("certificates/tls", &format!(r#"{{"cer":"{cer}"}}"#)),
        ]);
        let uris = [
            "az-kv://one/secrets/db",
            "az-kv://one/secrets/api",
            "az-kv://one/secrets/db/v1",
            "az-kv://one/keys/sign",
            "az-kv://two/certificates/tls",
        ];
        let env = [
            ("ARM_TENANT_ID", "11111111-1111-1111-1111-111111111111"),
            ("ARM_SUBSCRIPTION_ID", "sub-a"),
        ];
        let values = read(&p, &v, &uris, &[], &env).unwrap();
        assert_eq!(values[..3], ["s3cret", "k\ney \"q\"", "old"]);
        assert_eq!(values[3], r#"{"e":"AQAB","kty":"RSA","n":"AQAB"}"#);
        assert_eq!(values[4], pem(&der));
        let az = az_calls(&p);
        assert_eq!(az.lines().count(), 1, "{az}");
        assert!(
            az.contains("--tenant 11111111-1111-1111-1111-111111111111") && az.contains("--subscription sub-a"),
            "{az}"
        );
        assert!(
            v.requests
                .lock()
                .unwrap()
                .contains(&"auth two.vault.azure.net/certificates/tls".to_owned())
        );
    }

    #[test]
    fn certificates_are_pem() {
        let der: Vec<u8> = (0..=100).collect();
        let text = pem(&der);
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(
            (lines[0], lines[lines.len() - 1]),
            ("-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----")
        );
        assert_eq!(lines[1].len(), 64);
        let body: String = lines[1..lines.len() - 1].concat();
        assert_eq!(base64::engine::general_purpose::STANDARD.decode(body).unwrap(), der);
    }

    #[test]
    fn settings_override_the_banner_env_and_pick_the_cloud() {
        let p = with_fake_az();
        let v = vault(&[("secrets/db", r#"{"value":"s"}"#)]);
        let settings = [
            ("tenant", "22222222-2222-2222-2222-222222222222"),
            ("subscription", "sub-b"),
            ("vault_url", "https://{vault}.vault.azure.cn/"),
        ];
        let env = [("ARM_TENANT_ID", "11111111-1111-1111-1111-111111111111")];
        assert_eq!(
            read(&p, &v, &["az-kv://cn/secrets/db"], &settings, &env).unwrap(),
            ["s"]
        );
        let az = az_calls(&p);
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
        assert_eq!(key_vault(&["az-kv://kv/secrets/db"], access).unwrap(), ["s"]);
        assert!(!az_calls(&p).contains("--tenant"));
    }

    #[test]
    fn failures_name_the_reference() {
        let p = with_fake_az();
        let v = vault(&[
            ("secrets/db", r#"{"value":"s"}"#),
            ("secrets/none", "{}"),
            ("keys/none", "{}"),
            ("certificates/none", "{}"),
        ]);
        let err = |uris: &[&str], settings: &[(&str, &str)]| read(&p, &v, uris, settings, &[]).unwrap_err();
        assert_eq!(
            err(&["az-kv://kv/secrets/db"], &[("region", "x")]),
            "unknown az-kv setting `region` (known: tenant, subscription, vault_url)"
        );
        assert!(err(&["az-kv://kv/db"], &[]).contains("is not az-kv://"));
        let bad_tenant = err(&["az-kv://kv/secrets/db"], &[("tenant", "not a tenant!")]);
        assert!(bad_tenant.starts_with("Azure CLI sign-in settings: "), "{bad_tenant}");
        for kind in ["secrets", "keys", "certificates"] {
            let uri = format!("az-kv://kv/{kind}/x");
            assert!(
                err(&[&uri], &[("vault_url", "not a url {vault}")]).starts_with(&format!("{uri}: ")),
                "{kind}"
            );
            assert!(err(&[&uri], &[]).starts_with(&format!("{uri}: ")), "{kind}");
            let none = format!("az-kv://kv/{kind}/none");
            assert_eq!(err(&[&none], &[]), format!("{none}: Key Vault returned no value"));
        }
        let second = err(&["az-kv://kv/secrets/db", "az-kv://kv/secrets/missing"], &[]);
        assert!(second.starts_with("az-kv://kv/secrets/missing: "), "{second}");
        p.write("bin/FAIL", "");
        let signed_out = err(&["az-kv://kv/secrets/db"], &[]);
        assert!(
            signed_out.starts_with("az-kv://kv/secrets/db: ") && signed_out.contains("az login"),
            "{signed_out}"
        );
    }
}
