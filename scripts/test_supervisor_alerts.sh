#!/bin/bash
# =============================================================================
# Test Supervisor Alerts — trigger loi that tren Target Server
# Chay tren AGENT SERVER (172.16.50.93)
#
# Usage:
#   bash scripts/test_supervisor_alerts.sh [test_name]
#   bash scripts/test_supervisor_alerts.sh crash      # test CODE_ERR
#   bash scripts/test_supervisor_alerts.sh depfail    # test DEP_FAIL
#   bash scripts/test_supervisor_alerts.sh configerr  # test CONFIG_ERR
#   bash scripts/test_supervisor_alerts.sh permfail   # test PERM_ERR
#   bash scripts/test_supervisor_alerts.sh all        # chay tat ca
# =============================================================================

AGENT_URL="${AGENT_URL:-http://localhost:8082}"
TARGET_HOST="${TARGET_HOST:-172.16.50.147}"
SSH_USER="${SSH_USER:-devops}"
PROCESS_NAME="demo-worker"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

send_alert() {
    local alert_name="$1"
    local severity="$2"
    local process="$3"
    local summary="$4"

    echo -e "${CYAN}[ALERT]${NC} Gui alert: $alert_name cho process=$process"

    RESP=$(curl -s -w "\n%{http_code}" -X POST "$AGENT_URL/api/v1/alerts/webhook" \
        -H "Content-Type: application/json" \
        -d "{
            \"status\": \"firing\",
            \"alerts\": [{
                \"status\": \"firing\",
                \"labels\": {
                    \"alertname\": \"$alert_name\",
                    \"instance\": \"$TARGET_HOST:9100\",
                    \"severity\": \"$severity\",
                    \"job\": \"supervisor-exporter\",
                    \"process_name\": \"$process\",
                    \"group_name\": \"$process\"
                },
                \"annotations\": {
                    \"summary\": \"$summary\"
                },
                \"fingerprint\": \"test-sup-$(date +%s)-$RANDOM\"
            }]
        }")

    CODE=$(echo "$RESP" | tail -1)
    BODY=$(echo "$RESP" | head -n -1)

    if [ "$CODE" == "200" ]; then
        INC_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); ids=d.get('incident_ids',[]); print(ids[0] if ids else '')" 2>/dev/null)
        if [ -n "$INC_ID" ]; then
            echo -e "${GREEN}[OK]${NC} Incident created: $INC_ID"
            echo "     Xem chi tiet: $AGENT_URL/api/v1/incidents/$INC_ID"
        else
            echo -e "${YELLOW}[DEDUP]${NC} Alert bi dedup (da co incident truoc do)"
        fi
    else
        echo -e "${RED}[FAIL]${NC} HTTP $CODE: $BODY"
    fi
    echo ""
}

test_crash() {
    echo -e "${YELLOW}=== TEST: CODE_ERR (Unhandled Exception) ===${NC}"
    echo "Trigger: tao file flag de demo-worker crash voi RuntimeError"
    echo ""

    # Trigger crash tren Target Server
    echo -e "${CYAN}[SSH]${NC} Trigger crash tren $TARGET_HOST..."
    ssh "$SSH_USER@$TARGET_HOST" "touch /tmp/demo-worker-crash" 2>/dev/null

    # Doi process crash va supervisor ghi log
    echo -e "${CYAN}[WAIT]${NC} Doi 5s cho process crash..."
    sleep 5

    # Kiem tra trang thai
    echo -e "${CYAN}[CHECK]${NC} Trang thai supervisor:"
    ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status demo-worker" 2>/dev/null
    echo ""

    # Gui alert
    send_alert "SupervisorProcessExited" "critical" "$PROCESS_NAME" \
        "Process demo-worker exited with code 1 (RuntimeError)"
}

test_depfail() {
    echo -e "${YELLOW}=== TEST: DEP_FAIL (Connection Refused) ===${NC}"
    echo "Trigger: demo-worker co gang connect den IP khong ton tai"
    echo ""

    ssh "$SSH_USER@$TARGET_HOST" "touch /tmp/demo-worker-depfail" 2>/dev/null
    echo -e "${CYAN}[WAIT]${NC} Doi 5s cho process crash..."
    sleep 5

    ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status demo-worker" 2>/dev/null
    echo ""

    send_alert "SupervisorProcessExited" "critical" "$PROCESS_NAME" \
        "Process demo-worker exited - connection refused to dependency"
}

test_configerr() {
    echo -e "${YELLOW}=== TEST: CONFIG_ERR (Missing Environment Variable) ===${NC}"
    echo "Trigger: demo-worker crash vi thieu env var DATABASE_URL"
    echo ""

    ssh "$SSH_USER@$TARGET_HOST" "touch /tmp/demo-worker-configerr" 2>/dev/null
    echo -e "${CYAN}[WAIT]${NC} Doi 5s cho process crash..."
    sleep 5

    ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status demo-worker" 2>/dev/null
    echo ""

    send_alert "SupervisorProcessExited" "warning" "$PROCESS_NAME" \
        "Process demo-worker exited with code 1 - KeyError"
}

test_permfail() {
    echo -e "${YELLOW}=== TEST: PERM_ERR (Permission Denied) ===${NC}"
    echo "Trigger: demo-worker crash vi khong co quyen doc /etc/shadow"
    echo ""

    ssh "$SSH_USER@$TARGET_HOST" "touch /tmp/demo-worker-permfail" 2>/dev/null
    echo -e "${CYAN}[WAIT]${NC} Doi 5s cho process crash..."
    sleep 5

    ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status demo-worker" 2>/dev/null
    echo ""

    send_alert "SupervisorProcessExited" "warning" "$PROCESS_NAME" \
        "Process demo-worker exited - PermissionError"
}

wait_and_show() {
    local inc_label="$1"
    echo -e "${CYAN}[PIPELINE]${NC} Doi worker xu ly... (xem log: docker compose logs -f worker)"
    echo ""
    echo "============================================"
    echo -e "Mo browser: ${GREEN}$AGENT_URL${NC}"
    echo "De xem ket qua RCA va remediation options"
    echo "============================================"
}

# Main
case "${1:-}" in
    crash)
        test_crash
        wait_and_show "CODE_ERR"
        ;;
    depfail)
        test_depfail
        wait_and_show "DEP_FAIL"
        ;;
    configerr)
        test_configerr
        wait_and_show "CONFIG_ERR"
        ;;
    permfail)
        test_permfail
        wait_and_show "PERM_ERR"
        ;;
    all)
        echo "============================================"
        echo " Chay tat ca test cases (cach nhau 30s)"
        echo "============================================"
        echo ""

        test_crash
        echo -e "${CYAN}[WAIT]${NC} Doi 30s truoc test tiep theo..."
        sleep 30

        # Restart process truoc khi test tiep
        ssh "$SSH_USER@$TARGET_HOST" "sudo supervisorctl restart demo-worker" 2>/dev/null
        sleep 3

        test_configerr
        echo -e "${CYAN}[WAIT]${NC} Doi 30s truoc test tiep theo..."
        sleep 30

        ssh "$SSH_USER@$TARGET_HOST" "sudo supervisorctl restart demo-worker" 2>/dev/null
        sleep 3

        test_permfail
        echo -e "${CYAN}[WAIT]${NC} Doi 30s truoc test tiep theo..."
        sleep 30

        ssh "$SSH_USER@$TARGET_HOST" "sudo supervisorctl restart demo-worker" 2>/dev/null
        sleep 3

        test_depfail

        wait_and_show "ALL"
        ;;
    status)
        echo "Trang thai demo-worker tren Target Server:"
        ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status demo-worker"
        echo ""
        echo "Stderr log (10 dong cuoi):"
        ssh "$SSH_USER@$TARGET_HOST" "tail -10 /var/log/supervisor/demo-worker.err.log 2>/dev/null || echo '(khong co)'"
        echo ""
        echo "Stdout log (10 dong cuoi):"
        ssh "$SSH_USER@$TARGET_HOST" "tail -10 /var/log/supervisor/demo-worker.out.log 2>/dev/null || echo '(khong co)'"
        ;;
    restart)
        echo "Restart demo-worker..."
        ssh "$SSH_USER@$TARGET_HOST" "sudo supervisorctl restart demo-worker"
        ;;
    *)
        echo "Usage: $0 {crash|depfail|configerr|permfail|all|status|restart}"
        echo ""
        echo "  crash     - Test CODE_ERR: unhandled exception"
        echo "  depfail   - Test DEP_FAIL: connection refused"
        echo "  configerr - Test CONFIG_ERR: missing env variable"
        echo "  permfail  - Test PERM_ERR: permission denied"
        echo "  all       - Chay tat ca test cases"
        echo "  status    - Xem trang thai demo-worker tren Target"
        echo "  restart   - Restart demo-worker tren Target"
        echo ""
        echo "Env vars:"
        echo "  AGENT_URL=$AGENT_URL"
        echo "  TARGET_HOST=$TARGET_HOST"
        echo "  SSH_USER=$SSH_USER"
        ;;
esac
