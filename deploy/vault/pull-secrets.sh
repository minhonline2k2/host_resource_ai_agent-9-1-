#!/bin/bash
[ -z "$VAULT_ADDR" ] && echo "Set VAULT_ADDR and VAULT_TOKEN" && exit 1
echo "Pulling secrets from Vault..."

vault kv get -format=json secret/ai-alert-platform/infra 2>/dev/null | \
  jq -r '.data.data | to_entries[] | "\(.key | ascii_upcase)=\(.value)"' | \
  sed 's/^DB_USER/MYSQL_USER/;s/^DB_PASSWORD/MYSQL_PASSWORD/;s/^DB_ROOT_PASSWORD/MYSQL_ROOT_PASSWORD/' \
  > ../docker/.env.infra && echo "✅ .env.infra"

vault kv get -format=json secret/ai-alert-platform/orchestrator 2>/dev/null | \
  jq -r '.data.data | to_entries[] | "\(.key | ascii_upcase)=\(.value)"' \
  > ../docker/.env.orchestrator && echo "✅ .env.orchestrator"

vault kv get -format=json secret/ai-alert-platform/host-resource-agent 2>/dev/null | \
  jq -r '.data.data | to_entries[] | "\(.key | ascii_upcase)=\(.value)"' \
  > ../docker/.env.agent && echo "✅ .env.agent"

echo "Done! Run: cd ../docker && docker-compose up -d --build"
