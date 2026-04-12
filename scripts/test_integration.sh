#!/bin/bash
# =============================================================================
# Integration Test Script for Host Resource AI Agent
# Chạy sau khi docker compose up thành công
# Usage: bash scripts/test_integration.sh [BASE_URL]
# =============================================================================

BASE_URL="${1:-http://localhost:8082}"
PASS=0
FAIL=0
TOTAL=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check() {
    local name="$1"
    local expected_code="$2"
    local actual_code="$3"
    local body="$4"
    TOTAL=$((TOTAL + 1))

    if [ "$actual_code" == "$expected_code" ]; then
        echo -e "${GREEN}[PASS]${NC} $name (HTTP $actual_code)"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}[FAIL]${NC} $name (expected $expected_code, got $actual_code)"
        echo "  Response: ${body:0:200}"
        FAIL=$((FAIL + 1))
    fi
}

echo "============================================"
echo "Integration Tests — $BASE_URL"
echo "============================================"
echo ""

# --- Test 1: Health Check ---
echo -e "${YELLOW}=== Service Health ===${NC}"
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/health")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "Health check" "200" "$CODE" "$BODY"

# --- Test 2: UI loads ---
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/")
CODE=$(echo "$RESP" | tail -1)
check "UI index.html loads" "200" "$CODE"

# --- Test 3: List incidents (empty or with data) ---
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/incidents")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "List incidents" "200" "$CODE" "$BODY"

# --- Test 4: Incident stats ---
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/incidents/stats")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "Incident stats" "200" "$CODE" "$BODY"

# --- Test 5: Audit log ---
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/audit")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "Audit log" "200" "$CODE" "$BODY"

# --- Test 6: Send a test alert webhook (CPU High) ---
echo ""
echo -e "${YELLOW}=== Alert Webhook ===${NC}"
ALERT_PAYLOAD='{
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "HostCPUHigh",
        "instance": "10.0.1.99:9100",
        "severity": "warning",
        "job": "node-exporter",
        "env": "test"
      },
      "annotations": {
        "summary": "CPU usage is above 85% on 10.0.1.99:9100",
        "description": "CPU has been above 85% for more than 5 minutes."
      },
      "startsAt": "2026-04-12T10:00:00Z",
      "fingerprint": "test_cpu_integration_001"
    }
  ]
}'

RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/alerts/webhook" \
  -H "Content-Type: application/json" \
  -d "$ALERT_PAYLOAD")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "Webhook CPU alert" "200" "$CODE" "$BODY"

# Extract incident ID from response
INC_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('incident_ids',[''])[0])" 2>/dev/null)

if [ -n "$INC_ID" ] && [ "$INC_ID" != "" ]; then
    echo "  Created incident: $INC_ID"

    # --- Test 7: Get incident detail ---
    sleep 1
    RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/incidents/$INC_ID")
    CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | head -n -1)
    check "Get incident detail" "200" "$CODE" "$BODY"

    # --- Test 8: Test dedup (same fingerprint should not create new incident) ---
    RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/alerts/webhook" \
      -H "Content-Type: application/json" \
      -d "$ALERT_PAYLOAD")
    CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | head -n -1)
    DEDUP_COUNT=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('incidents_created',1))" 2>/dev/null)
    TOTAL=$((TOTAL + 1))
    if [ "$DEDUP_COUNT" == "0" ]; then
        echo -e "${GREEN}[PASS]${NC} Dedup works (0 new incidents on duplicate)"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}[FAIL]${NC} Dedup check (expected 0 new, got $DEDUP_COUNT)"
        FAIL=$((FAIL + 1))
    fi

    # --- Test 9: Skip LLM ---
    RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/incidents/$INC_ID/skip-llm")
    CODE=$(echo "$RESP" | tail -1)
    check "Skip LLM" "200" "$CODE"

    # --- Test 10: Monitor incident ---
    RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/incidents/$INC_ID/monitor" \
      -H "Content-Type: application/json" \
      -d '{"duration_minutes": 5}')
    CODE=$(echo "$RESP" | tail -1)
    check "Monitor incident" "200" "$CODE"

    # --- Test 11: Delete test incident ---
    RESP=$(curl -s -w "\n%{http_code}" -X DELETE "$BASE_URL/api/v1/incidents/$INC_ID")
    CODE=$(echo "$RESP" | tail -1)
    check "Delete incident" "200" "$CODE"

else
    echo -e "${YELLOW}  [SKIP]${NC} No incident created — skipping detail/dedup/action tests"
fi

# --- Test 12: Send Supervisor alert ---
echo ""
echo -e "${YELLOW}=== Supervisor Alert ===${NC}"
SUP_PAYLOAD='{
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "SupervisorProcessDown",
        "instance": "10.0.1.99:9100",
        "severity": "critical",
        "process_name": "celery-worker",
        "job": "supervisor-exporter"
      },
      "annotations": {
        "summary": "Supervisor process celery-worker is DOWN"
      },
      "fingerprint": "test_supervisor_integration_001"
    }
  ]
}'

RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/alerts/webhook" \
  -H "Content-Type: application/json" \
  -d "$SUP_PAYLOAD")
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
check "Webhook Supervisor alert" "200" "$CODE" "$BODY"

# Cleanup supervisor test incident
SUP_INC_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('incident_ids',[''])[0])" 2>/dev/null)
if [ -n "$SUP_INC_ID" ] && [ "$SUP_INC_ID" != "" ]; then
    curl -s -X DELETE "$BASE_URL/api/v1/incidents/$SUP_INC_ID" > /dev/null 2>&1
fi

# --- Test 13: Invalid approval ---
echo ""
echo -e "${YELLOW}=== Error Handling ===${NC}"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/api/v1/approvals" \
  -H "Content-Type: application/json" \
  -d '{"action_proposal_id": "nonexistent-id", "decision": "approved"}')
CODE=$(echo "$RESP" | tail -1)
check "Invalid approval returns 404" "404" "$CODE"

# --- Test 14: Get nonexistent incident ---
RESP=$(curl -s -w "\n%{http_code}" "$BASE_URL/api/v1/incidents/nonexistent-id")
CODE=$(echo "$RESP" | tail -1)
check "Nonexistent incident returns 404" "404" "$CODE"

# --- Summary ---
echo ""
echo "============================================"
echo -e "Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, $TOTAL total"
echo "============================================"

if [ $FAIL -gt 0 ]; then
    exit 1
fi
