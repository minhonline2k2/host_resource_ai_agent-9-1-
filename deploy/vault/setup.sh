#!/bin/bash
set -e
echo "🔐 Vault Setup for AI Alert Platform"
echo "   VAULT_ADDR: ${VAULT_ADDR:-(not set!)}"
[ -z "$VAULT_ADDR" ] || [ -z "$VAULT_TOKEN" ] && echo "❌ Set VAULT_ADDR and VAULT_TOKEN" && exit 1

vault secrets enable -path=secret kv-v2 2>/dev/null || true

vault kv put secret/ai-alert-platform/orchestrator \
    database_url="mysql+asyncmy://ai_bot:CHANGE_ME@db:3306/ai_alert_platform?charset=utf8mb4" \
    redis_url="redis://:CHANGE_ME@redis:6379/0" \
    teams_webhook_url="CHANGE_ME" \
    ui_base_url="http://192.168.200.187:8080" \
    secret_key="$(openssl rand -hex 32)"

vault kv put secret/ai-alert-platform/host-resource-agent \
    database_url="mysql+asyncmy://ai_bot:CHANGE_ME@db:3306/ai_alert_platform?charset=utf8mb4" \
    redis_url="redis://:CHANGE_ME@redis:6379/0" \
    prometheus_url="http://192.168.200.187:9090" \
    ssh_user="hungnv1" ssh_key_path="/app/ssh_keys/id_rsa" \
    gemini_api_key="CHANGE_ME" gemini_model="gemini-2.5-flash" \
    llm_timeout="360" orchestrator_url="http://orchestrator:8080"

vault kv put secret/ai-alert-platform/infra \
    db_root_password="CHANGE_ME" db_user="ai_bot" db_password="CHANGE_ME" redis_password="CHANGE_ME"

vault policy write ai-alert-platform - << 'POLICY'
path "secret/data/ai-alert-platform/*" { capabilities = ["read"] }
POLICY

echo "✅ Done! Update CHANGE_ME values then run ./pull-secrets.sh"
