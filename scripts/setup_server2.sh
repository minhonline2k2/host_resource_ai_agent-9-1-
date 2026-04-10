#!/bin/bash
# =============================================================================
# Setup script cho Server 2 (TARGET HOST): 172.16.50.147
# Chạy script này TRÊN Server 2
# =============================================================================
set -e

echo "============================================"
echo "  Setup Server 2 (Target) - 172.16.50.147"
echo "============================================"
echo ""

# --- 1. Cài Node Exporter ---
echo "[1/4] Cài đặt Node Exporter..."

NODE_EXPORTER_VERSION="1.8.1"
cd /tmp

if ! command -v /usr/local/bin/node_exporter &> /dev/null; then
    wget -q "https://github.com/prometheus/node_exporter/releases/download/v${NODE_EXPORTER_VERSION}/node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
    tar xzf "node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"
    sudo cp "node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter" /usr/local/bin/
    rm -rf "node_exporter-${NODE_EXPORTER_VERSION}.linux-amd64"*
    echo "   ✅ Node Exporter ${NODE_EXPORTER_VERSION} đã cài"
else
    echo "   ✅ Node Exporter đã có sẵn"
fi

# Tạo user cho node_exporter
sudo useradd --no-create-home --shell /bin/false node_exporter 2>/dev/null || true

# Tạo systemd service
sudo tee /etc/systemd/system/node_exporter.service > /dev/null <<'EOF'
[Unit]
Description=Node Exporter
Documentation=https://prometheus.io/docs/guides/node-exporter/
After=network-online.target

[Service]
User=node_exporter
ExecStart=/usr/local/bin/node_exporter \
    --collector.filesystem.mount-points-exclude="^/(sys|proc|dev|run)($|/)" \
    --collector.netclass.ignored-devices="^(veth|docker|br-).*"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter
echo "   ✅ Node Exporter đang chạy trên port 9100"

# --- 2. Tạo user devops cho SSH ---
echo ""
echo "[2/4] Tạo user devops cho SSH..."

if ! id "devops" &>/dev/null; then
    sudo useradd -m -s /bin/bash devops
    echo "   ✅ User devops đã tạo"
else
    echo "   ✅ User devops đã tồn tại"
fi

sudo mkdir -p /home/devops/.ssh
sudo chmod 700 /home/devops/.ssh
sudo chown devops:devops /home/devops/.ssh

# --- 3. Cấp sudo cho devops ---
echo ""
echo "[3/4] Cấp quyền sudo cho devops..."

echo "devops ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/devops > /dev/null
sudo chmod 0440 /etc/sudoers.d/devops
echo "   ✅ devops có quyền sudo NOPASSWD"

# --- 4. Cài các tool cần thiết ---
echo ""
echo "[4/4] Cài đặt các tool cần thiết..."

# Detect package manager
if command -v apt-get &> /dev/null; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq sysstat lsof net-tools procps > /dev/null 2>&1
elif command -v yum &> /dev/null; then
    sudo yum install -y -q sysstat lsof net-tools procps-ng > /dev/null 2>&1
elif command -v dnf &> /dev/null; then
    sudo dnf install -y -q sysstat lsof net-tools procps-ng > /dev/null 2>&1
fi
echo "   ✅ sysstat, lsof, net-tools đã cài"

# --- Done ---
echo ""
echo "============================================"
echo "  ✅ Server 2 setup HOÀN TẤT!"
echo "============================================"
echo ""
echo "Kiểm tra:"
echo "  1. Node Exporter: curl http://localhost:9100/metrics | head"
echo "  2. SSH:           ssh devops@172.16.50.147"
echo ""
echo "⚠️  BƯỚC TIẾP THEO:"
echo "  Chạy lệnh sau TRÊN Server 1 (172.16.50.93) để copy SSH key:"
echo "  ssh-copy-id -i ~/.ssh/id_rsa.pub devops@172.16.50.147"
echo ""
