#!/bin/bash -xe

THIS_DIR=$(dirname "${BASH_SOURCE[0]}")
PROJECT_ROOT=$(realpath "${THIS_DIR}/..")
cd "${PROJECT_ROOT}"

LOCAL_BIN_PATH="${HOME}/.local/bin"
case ":${PATH}:" in
    *":${LOCAL_BIN_PATH}:"*) ;;
    *) export PATH="${LOCAL_BIN_PATH}:${PATH}" ;;
esac

export SERVER_BASE_URL="${SERVER_BASE_URL:-http://localhost:8000}"

export CH_API_API_KEY="${CH_API_API_KEY:-op://Employee/Companies House API keys/Test Key}"

export AUTH0_MODE=${AUTH0_MODE:-none}
export AUTH0_DOMAIN="${AUTH0_DOMAIN:-op://Auth0 - dev/MCP Auth0 App/OAuth Domain}"
export AUTH0_AUDIENCE="${AUTH0_AUDIENCE:-op://Auth0 - dev/MCP Auth0 App/OAuth Audience}"
export AUTH0_CLIENT_ID="${AUTH0_CLIENT_ID:-op://Auth0 - dev/MCP Auth0 App/OAuth Client ID}"
export AUTH0_CLIENT_SECRET="${AUTH0_CLIENT_SECRET:-op://Auth0 - dev/MCP Auth0 App/OAuth Client Secret}"
export AUTH0_JWT_SIGNING_KEY="secretsecretsecretsecretsecretsecretsecretsecretsecretsecretsecretsecret"
export AUTH0_STORAGE_ENCRYPTION_KEY="YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE="

export AZURITE_HOSTNAME="${AZURITE_HOSTNAME:-localhost}"
export DEFAULT_AzureWebJobsStorage="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://${AZURITE_HOSTNAME}:10000/devstoreaccount1;QueueEndpoint=http://${AZURITE_HOSTNAME}:10001/devstoreaccount1;TableEndpoint=http://${AZURITE_HOSTNAME}:10002/devstoreaccount1"
export AZURE_STORAGE_CONNECTION_STRING="${AZURE_STORAGE_CONNECTION_STRING:-${DEFAULT_AzureWebJobsStorage}}"
export AZURE_CREDENTIAL="${AZURE_CREDENTIAL:-none}"

export HUMAN_LOGS="1"
export SERVER_JWT_SECRET_KEY="11111111111111111111111111111111"
export DEBUG="true"

if [ -n "$*" ]; then
    ARGS=("$@")
else
    ARGS=(python -m ch_mcp serve --reload)
fi

cd "${THIS_DIR}/.."
exec op run --no-masking -- pdm run "${ARGS[@]}"