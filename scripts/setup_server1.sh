#!/bin/bash
# =============================================================================
# Setup script cho Server 1 (AI AGENT): 172.16.50.93
# Chạy script này TRÊN Server 1
# =============================================================================
set -e

echo "============================================"
echo "  Setup Server 1 (Agent) - 172.16.50.93"
echo "============================================"
echo ""

SERVER2_IP="172.16.50.147"
SSH_USER="devops"

# --- 1. Cài Docker & Docker Compose ---
echo "[1/5] Kiểm tra Docker..."

if ! command -v docker &> /dev/null; then
    echo "   Đang cài Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker $USER
    echo "   ✅ Docker đã cài. Cần logout/login lại để dùng docker không cần sudo."
else
    echo "   ✅ Docker đã có: $(docker --version)"
fi

if ! command -v docker compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "   Đang cài Docker Compose plugin..."
    sudo apt-get install -y docker-compose-plugin 2>/dev/null || \
    sudo yum install -y docker-compose-plugin 2>/dev/null || true
fi
echo "   ✅ Docker Compose OK"

# --- 2. Tạo SSH keypair ---
echo ""
echo "[2/5] Tạo SSH keypair..."

if [ ! -f ~/.ssh/id_rsa ]; then
    ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -N "" -q
    echo "   ✅ SSH keypair đã tạo: ~/.ssh/id_rsa"
else
    echo "   ✅ SSH keypair đã tồn tại: ~/.ssh/id_rsa"
fi

# --- 3. Copy SSH key sang Server 2 ---
echo ""
echo "[3/5] Copy SSH key sang Server 2 (${SERVER2_IP})..."
echo "   ⚠️  Sẽ hỏi password của user ${SSH_USER} trên ${SERVER2_IP}"
echo ""

ssh-copy-id -o StrictHostKeyChecking=no -i ~/.ssh/id_rsa.pub ${SSH_USER}@${SERVER2_IP} 2>/dev/null || {
    echo "   ❌ Không thể copy SSH key tự động."
    echo "   Chạy thủ công: ssh-copy-id -i ~/.ssh/id_rsa.pub ${SSH_USER}@${SERVER2_IP}"
}

# Test SSH
echo "   Testing SSH connection..."
if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i ~/.ssh/id_rsa ${SSH_USER}@${SERVER2_IP} "echo 'SSH OK'" 2>/dev/null; then
    echo "   ✅ SSH connection OK!"
else
    echo "   ❌ SSH connection failed. Kiểm tra lại setup Server 2."
fi

# --- 4. Tạo database ---
echo ""
echo "[4/5] Chuẩn bị project..."

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)
echo "   Project dir: ${PROJECT_DIR}"

# Tạo thư mục prometheus nếu chưa có
mkdir -p prometheus

# --- 5. Khởi động Docker Compose ---
echo ""
echo "[5/5] Khởi động Docker Compose..."
echo ""

docker compose up -d --build

echo ""
echo "============================================"
echo "  ✅ Server 1 setup HOÀN TẤT!"
echo "============================================"
echo ""
echo "Services đang chạy:"
echo "  🌐 Agent UI:     http://172.16.50.93:8082"
echo "  📡 API Docs:     http://172.16.50.93:8082/docs"
echo "  📊 Prometheus:   http://172.16.50.93:9090"
echo "  🔔 Alertmanager: http://172.16.50.93:9093"
echo ""
echo "Kiểm tra logs:"
echo "  docker compose logs -f app"
echo "  docker compose logs -f worker"
echo ""
echo "Test gửi alert:"
echo '  curl -X POST http://localhost:8082/api/v1/alerts/webhook \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"HostCPUHigh","instance":"172.16.50.147:9100","severity":"warning","job":"node-exporter"},"annotations":{"summary":"CPU usage is above 90%"},"fingerprint":"test-001"}]}'"'"''
echo ""
