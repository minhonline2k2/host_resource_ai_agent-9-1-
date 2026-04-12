#!/bin/bash
# =============================================================================
# REAL SCENARIO TEST: Xoa config file -> api-service crash -> AI agent phan tich
# -> Operator approve -> Commands chay that -> Service phuc hoi
#
# Chay tren AGENT SERVER (172.16.50.93)
# =============================================================================

AGENT_URL="${AGENT_URL:-http://localhost:8082}"
TARGET_HOST="${TARGET_HOST:-172.16.50.147}"
SSH_USER="${SSH_USER:-devops}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} REAL SCENARIO: Config file bi xoa${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo "Kich ban:"
echo "  1. api-service dang chay binh thuong tren port 8888"
echo "  2. Config file bi XOA (mo phong loi that)"
echo "  3. api-service crash vi FileNotFoundError"
echo "  4. AI Agent thu thap evidence + goi LLM"
echo "  5. LLM tra ve root cause + remediation"
echo "  6. Operator review va approve tren UI"
echo "  7. Agent thuc thi commands -> service phuc hoi"
echo ""

# === BUOC 1: Kiem tra api-service dang chay ===
echo -e "${YELLOW}=== BUOC 1: Kiem tra api-service dang chay ===${NC}"
STATUS=$(ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status api-service 2>/dev/null" 2>/dev/null)
echo "  Status: $STATUS"

if echo "$STATUS" | grep -q "RUNNING"; then
    echo -e "  ${GREEN}OK — api-service dang RUNNING${NC}"
else
    echo -e "  ${RED}api-service chua chay. Chay setup truoc:${NC}"
    echo "  scp scripts/setup_real_test.sh $SSH_USER@$TARGET_HOST:/tmp/"
    echo "  ssh $SSH_USER@$TARGET_HOST 'bash /tmp/setup_real_test.sh'"
    exit 1
fi

# Test health
HEALTH=$(ssh "$SSH_USER@$TARGET_HOST" "curl -s http://localhost:8888/health 2>/dev/null")
echo "  Health: $HEALTH"
echo ""

# === BUOC 2: XOA CONFIG FILE (gay loi that) ===
echo -e "${YELLOW}=== BUOC 2: Xoa config file (gay loi THAT) ===${NC}"
echo -e "  ${CYAN}[SSH]${NC} mv /opt/test-apps/api_config.json /opt/test-apps/api_config.json.bak"
ssh "$SSH_USER@$TARGET_HOST" "mv /opt/test-apps/api_config.json /opt/test-apps/api_config.json.bak" 2>/dev/null
echo -e "  ${GREEN}Done — config file da bi xoa (backup o .bak)${NC}"
echo ""

# === BUOC 3: Restart de api-service doc lai config -> CRASH ===
echo -e "${YELLOW}=== BUOC 3: Restart api-service -> se CRASH vi thieu config ===${NC}"
echo -e "  ${CYAN}[SSH]${NC} supervisorctl restart api-service"
ssh "$SSH_USER@$TARGET_HOST" "supervisorctl restart api-service 2>/dev/null; sleep 3; supervisorctl status api-service" 2>/dev/null
echo ""

# Kiem tra da crash chua
sleep 2
STATUS=$(ssh "$SSH_USER@$TARGET_HOST" "supervisorctl status api-service 2>/dev/null" 2>/dev/null)
echo -e "  Status sau restart: ${RED}$STATUS${NC}"

# Xem stderr
echo ""
echo -e "  ${CYAN}Stderr log (5 dong cuoi):${NC}"
ssh "$SSH_USER@$TARGET_HOST" "tail -5 /var/log/supervisor/api-service.err.log 2>/dev/null" 2>/dev/null
echo ""

# === BUOC 4: Gui alert den AI Agent ===
echo -e "${YELLOW}=== BUOC 4: Gui alert den AI Agent ===${NC}"
RESP=$(curl -s -w "\n%{http_code}" -X POST "$AGENT_URL/api/v1/alerts/webhook" \
    -H "Content-Type: application/json" \
    -d "{
        \"status\": \"firing\",
        \"alerts\": [{
            \"status\": \"firing\",
            \"labels\": {
                \"alertname\": \"SupervisorProcessExited\",
                \"instance\": \"$TARGET_HOST:9100\",
                \"severity\": \"critical\",
                \"job\": \"supervisor-exporter\",
                \"process_name\": \"api-service\",
                \"group_name\": \"api-service\"
            },
            \"annotations\": {
                \"summary\": \"Process api-service exited with code 1 - FileNotFoundError\"
            },
            \"fingerprint\": \"real-test-$(date +%s)\"
        }]
    }")

CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)

if [ "$CODE" == "200" ]; then
    INC_ID=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); ids=d.get('incident_ids',[]); print(ids[0] if ids else '')" 2>/dev/null)
    echo -e "  ${GREEN}Incident created: $INC_ID${NC}"
else
    echo -e "  ${RED}FAIL: HTTP $CODE${NC}"
    exit 1
fi

echo ""
echo -e "${YELLOW}=== BUOC 5: Doi AI Agent xu ly (SSH + LLM) ===${NC}"
echo -e "  ${CYAN}Xem log:${NC} docker compose logs -f worker --since 1m"
echo ""
echo -e "${BOLD}============================================${NC}"
echo -e "${BOLD} TIEP THEO:${NC}"
echo -e "${BOLD}============================================${NC}"
echo ""
echo "1. Xem worker log: docker compose logs -f worker --since 2m"
echo ""
echo "2. Mo browser: ${GREEN}$AGENT_URL${NC}"
echo "   Click vao incident moi -> xem Root Cause + Phuong an"
echo ""
echo "3. LLM se phan tich:"
echo "   - Category: CONFIG_ERR"
echo "   - Root cause: FileNotFoundError /opt/test-apps/api_config.json"
echo "   - Remediation: tao lai config file + restart"
echo ""
echo "4. Approve phuong an tren UI"
echo ""
echo "5. Sau khi approve, kiem tra:"
echo "   ssh $SSH_USER@$TARGET_HOST 'supervisorctl status api-service'"
echo "   # Phai thay RUNNING neu fix thanh cong"
echo ""
echo -e "${YELLOW}=== NEU MUON RESET DE TEST LAI ===${NC}"
echo "   ssh $SSH_USER@$TARGET_HOST 'cp /opt/test-apps/api_config.json.bak /opt/test-apps/api_config.json && supervisorctl restart api-service'"
echo ""
