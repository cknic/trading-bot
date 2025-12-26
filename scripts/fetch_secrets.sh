#!/usr/bin/env bash
set -euo pipefail

: "${KEYVAULT_NAME:?Need KEYVAULT_NAME}"
OUT_ENV="/run/trading/secrets.env"

mkdir -p /run/trading
chmod 700 /run/trading

# login via managed identity (idempotent)
az login --identity >/dev/null 2>&1 || true

get_secret () {
  local name="$1"
  az keyvault secret show \
    --vault-name "$KEYVAULT_NAME" \
    --name "$name" \
    --query value -o tsv
}

umask 077
cat > "$OUT_ENV" << EOV
OPENAI_API_KEY=$(get_secret openai-api-key)
OPENROUTER_API_KEY=$(get_secret openrouter-api-key)
KRAKEN_API_KEY=$(get_secret kraken-api-key)
KRAKEN_API_SECRET=$(get_secret kraken-api-secret)
EOV

chmod 600 "$OUT_ENV"
echo "Wrote secrets to $OUT_ENV"
