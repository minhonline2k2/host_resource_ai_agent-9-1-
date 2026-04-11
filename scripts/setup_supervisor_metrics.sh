#!/bin/bash
# =================================================================
# Setup script for Supervisor Metrics on Server 2 (172.16.50.147)
# Chạy: sudo bash scripts/setup_supervisor_metrics.sh
# =================================================================

set -e

echo "=== Setup Supervisor Metrics Exporter ==="

# 1. Tạo thư mục textfile collector
echo "[1/5] Creating textfile collector directory..."
sudo mkdir -p /var/lib/node_exporter/textfile_collector
sudo chown node_exporter:node_exporter /var/lib/node_exporter/textfile_collector 2>/dev/null || true

# 2. Copy script
echo "[2/5] Installing supervisor_metrics.sh..."
SCRIPT_PATH="/usr/local/bin/supervisor_metrics.sh"
sudo cp scripts/supervisor_metrics.sh "$SCRIPT_PATH"
sudo chmod +x "$SCRIPT_PATH"

# 3. Test script
echo "[3/5] Testing metrics collection..."
sudo bash "$SCRIPT_PATH"
echo "--- Metrics output ---"
cat /var/lib/node_exporter/textfile_collector/supervisor.prom
echo "--- End ---"

# 4. Cấu hình node_exporter textfile collector
echo "[4/5] Configuring node_exporter with textfile collector..."

# Kiểm tra node_exporter đã có --collector.textfile.directory chưa
if systemctl cat node_exporter 2>/dev/null | grep -q "textfile.directory"; then
    echo "  textfile collector already configured"
else
    # Sửa systemd service để thêm --collector.textfile.directory
    sudo tee /etc/systemd/system/node_exporter.service > /dev/null <<'EOF'
[Unit]
Description=Node Exporter
After=network-online.target

[Service]
User=node_exporter
ExecStart=/usr/local/bin/node_exporter --collector.textfile.directory=/var/lib/node_exporter/textfile_collector
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl restart node_exporter
    echo "  node_exporter restarted with textfile collector"
fi

# 5. Setup cron (mỗi 15 giây thông qua systemd timer — cron chỉ hỗ trợ tối thiểu 1 phút)
echo "[5/5] Setting up systemd timer for 15-second interval..."

sudo tee /etc/systemd/system/supervisor-metrics.service > /dev/null <<'EOF'
[Unit]
Description=Collect Supervisor Metrics

[Service]
Type=oneshot
ExecStart=/usr/local/bin/supervisor_metrics.sh
EOF

sudo tee /etc/systemd/system/supervisor-metrics.timer > /dev/null <<'EOF'
[Unit]
Description=Run Supervisor Metrics Collector every 15s

[Timer]
OnBootSec=10s
OnUnitActiveSec=15s
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now supervisor-metrics.timer

echo ""
echo "=== Setup Complete ==="
echo "Verify: curl -s http://localhost:9100/metrics | grep supervisor_process"
echo ""
