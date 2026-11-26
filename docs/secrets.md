# Secrets

Keep secrets out of your tfvars files by writing a *reference* where the value
goes. tfp looks each one up when a command needs the tfvars, so the files you
commit only ever hold references.

```hcl
# {"header": "terraform", "project": "api"}
db_password = "{{ op://Infra/postgres-prod/password }}"
api_key     = "{{ az-kv://api-prod-kv/api-key }}"
```

A reference is `{{ scheme://… }}`. The scheme picks where the secret comes
from:

| Scheme | Reference | Comes from |
| --- | --- | --- |
| `op://` | `op://<vault>/<item>/[<section>/]<field>` | 1Password |
| `az-kv://` | `az-kv://<vault>/<secret>[/<version>]` | Azure Key Vault |

References work anywhere in the file: as a whole value, inside a longer
string, or in a multi-line string or heredoc. Spaces inside the braces are
optional. Other `{{ … }}` text, such as a Go or Helm template, is left alone.

```hcl
app_config = <<EOT
listen: 8080
api_key: {{op://Infra/api/credential}}
greeting: {{ .Values.greeting }}
EOT
```

## What happens when you run a command

When `tfp plan`, `apply`, `refresh`, `destroy` or `import` needs the tfvars:

1. tfp finds every reference in the file.
2. It fetches all references of one scheme together: one 1Password call for
   all the `op://` ones, and one Azure sign-in per vault for `az-kv://`.
3. It writes a filled-in copy into a private temporary directory and gives
   that file to Terraform.
4. Afterwards it overwrites the copy with zeros and deletes it.

A tfvars file without references is used as it is.

Values are inserted exactly as stored. A secret containing a newline is fine
in a heredoc, but inside an ordinary `"…"` string a newline, `"` or `\` would
break the HCL.

`tfp self doctor` lists the schemes your tfvars use and checks that each
one's tool is installed. A reference with a scheme tfp doesn't know stops the
command with an error naming the file and the scheme.

## 1Password (`op://`)

Needs the [1Password CLI](https://developer.1password.com/docs/cli/get-started/),
signed in in one of the usual ways:

- on your machine, through the 1Password desktop app's CLI integration;
- in CI, with a service account token in `OP_SERVICE_ACCOUNT_TOKEN`.

All of a file's `op://` references are read in a single `op inject` call, so
you approve access once per command. The reference format is the one
1Password itself uses; *Copy Secret Reference* in the app gives you one.

## Azure Key Vault (`az-kv://`)

Needs the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli),
signed in with `az login`, and permission to read secrets in the vault. Name
the vault and the secret, and optionally a version (the latest otherwise):

```hcl
db_password = "{{ az-kv://api-prod-kv/db-password }}"
old_key     = "{{ az-kv://api-prod-kv/api-key/0123456789abcdef0123456789abcdef }}"
```

tfp signs in once per vault and then reads that vault's secrets at the same
time.

Settings go in the banner's `secrets_config`, under `"az-kv"`. They're often
unnecessary: tfp falls back to the `ARM_TENANT_ID` and `ARM_SUBSCRIPTION_ID`
that Azure banners usually carry in `env` already.

```hcl
# {"header": "terraform", "project": "api",
#  "env": {"ARM_SUBSCRIPTION_ID": "…"},
#  "secrets_config": {"az-kv": {"tenant": "…"}}}
```

| Setting | Default | Use it when |
| --- | --- | --- |
| `tenant` | the banner's `env.ARM_TENANT_ID` | The vault is in a different tenant from your `az` login's default. |
| `subscription` | the banner's `env.ARM_SUBSCRIPTION_ID` | You need `az` to use a particular subscription's account. |
| `vault_url` | `https://{vault}.vault.azure.net/` | The vault is in a sovereign cloud, e.g. `https://{vault}.vault.azure.cn/`. |

If `az` isn't signed in, the error includes its own advice (usually "run
`az login`").

## Older configs: `[tf_project.secrets] command`

Before tfp resolved references itself, it ran an external command over the
whole file, by default `op inject`. That setting is deprecated:

- If your config still has the old default
  (`["op", "inject", "--in-file", "{in}", "--out-file", "{out}"]`), tfp uses
  the built-in resolution instead and asks you to delete the setting.
- Any other command still runs as before, with a warning. It will stop being
  supported in a later release.
