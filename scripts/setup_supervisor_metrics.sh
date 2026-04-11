#!/bin/bash
# =================================================================
# Setup Supervisor Metrics trên Server 2
# Cách dùng:
#   curl -fsSL https://raw.githubusercontent.com/minhonline2k2/host_resource_ai_agent-9-1-/main/scripts/setup_supervisor_metrics.sh | sudo bash
# =================================================================

set -e

REPO_RAW="https://raw.githubusercontent.com/minhonline2k2/host_resource_ai_agent-9-1-/main"

echo "=== [1/5] Tải supervisor_metrics.sh từ GitHub ==="
curl -fsSL -o /usr/local/bin/supervisor_metrics.sh "${REPO_RAW}/scripts/supervisor_metrics.sh"
chmod +x /usr/local/bin/supervisor_metrics.sh
echo "  ✅ Đã cài /usr/local/bin/supervisor_metrics.sh"

echo ""
echo "=== [2/5] Tạo thư mục textfile collector ==="
mkdir -p /var/lib/node_exporter/textfile_collector
echo "  ✅ /var/lib/node_exporter/textfile_collector"

echo ""
echo "=== [3/5] Test thu thập metrics ==="
bash /usr/local/bin/supervisor_metrics.sh
echo "--- Kết quả ---"
cat /var/lib/node_exporter/textfile_collector/supervisor.prom
echo "--- Hết ---"

echo ""
echo "=== [4/5] Cấu hình node_exporter textfile collector ==="
# Sửa systemd service thêm --collector.textfile.directory
cat > /etc/systemd/system/node_exporter.service <<'SVC'
[Unit]
Description=Node Exporter
After=network-online.target

[Service]
User=root
ExecStart=/usr/local/bin/node_exporter --collector.textfile.directory=/var/lib/node_exporter/textfile_collector
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl restart node_exporter
echo "  ✅ node_exporter đã restart với textfile collector"

echo ""
echo "=== [5/5] Cài timer chạy mỗi 15 giây ==="
cat > /etc/systemd/system/supervisor-metrics.service <<'SVC'
[Unit]
Description=Collect Supervisor Metrics
[Service]
Type=oneshot
ExecStart=/usr/local/bin/supervisor_metrics.sh
SVC

cat > /etc/systemd/system/supervisor-metrics.timer <<'TMR'
[Unit]
Description=Run Supervisor Metrics every 15s
[Timer]
OnBootSec=10s
OnUnitActiveSec=15s
AccuracySec=1s
[Install]
WantedBy=timers.target
TMR

systemctl daemon-reload
systemctl enable --now supervisor-metrics.timer
echo "  ✅ Timer đã bật — metrics cập nhật mỗi 15s"

echo ""
echo "=========================================="
echo "✅ SETUP HOÀN TẤT!"
echo "=========================================="
echo ""
echo "Kiểm tra: curl -s http://localhost:9100/metrics | grep supervisor_process"
echo ""
