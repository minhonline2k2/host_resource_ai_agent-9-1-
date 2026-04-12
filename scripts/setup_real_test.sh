#!/bin/bash
# =============================================================================
# Tao 1 service THAT tren Target Server: api-service
# Day la 1 Python HTTP API server that su:
#   - Doc config tu file YAML
#   - Bind port 8888
#   - Ghi log ra stdout
#   - Crash THAT khi config bi xoa/hong
#
# Usage: chay tren TARGET SERVER (172.16.50.147)
#   bash setup_real_test.sh
# =============================================================================

set -e

echo "============================================"
echo " Setup api-service (real test)"
echo "============================================"

# 1. Tao app
echo "[1/4] Tao api-service app..."
cat > /opt/test-apps/api_service.py << 'PYEOF'
#!/usr/bin/env python3
"""
Real API service - khong co flag file, khong co trick.
Doc config -> bind port -> serve HTTP.
Crash THAT khi config sai/mat.
"""
import http.server
import json
import os
import sys
import time
import signal

CONFIG_PATH = "/opt/test-apps/api_config.json"

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [api-service] {msg}", flush=True)

def load_config():
    """Load config - crash that neu file khong ton tai hoac sai format."""
    log(f"Loading config from {CONFIG_PATH}")
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)

    # Validate required fields
    required = ["host", "port", "db_connection", "workers"]
    for field in required:
        if field not in config:
            raise ValueError(f"Missing required config field: {field}")

    log(f"Config loaded: host={config['host']}, port={config['port']}, workers={config['workers']}")
    return config

class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "pid": os.getpid()}).encode())
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        log(f"HTTP {args[0]}")

def main():
    log(f"Starting api-service PID={os.getpid()}")
    log(f"Python {sys.version}")

    config = load_config()

    host = config["host"]
    port = config["port"]

    log(f"Binding to {host}:{port}...")
    server = http.server.HTTPServer((host, port), HealthHandler)
    log(f"api-service is listening on {host}:{port}")
    log(f"Health check: http://{host}:{port}/health")

    server.serve_forever()

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda s,f: (log("SIGTERM received, stopping..."), sys.exit(0)))
    try:
        main()
    except Exception as e:
        # In ra stderr - day la cai LLM se doc
        print(f"FATAL ERROR: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
PYEOF

# 2. Tao config file DUNG
echo "[2/4] Tao config file..."
cat > /opt/test-apps/api_config.json << 'EOF'
{
    "host": "0.0.0.0",
    "port": 8888,
    "db_connection": "mysql://app:pass@localhost:3306/mydb",
    "workers": 4,
    "log_level": "INFO",
    "max_connections": 100
}
EOF

# 3. Tao supervisor config
echo "[3/4] Tao supervisor config..."
sudo tee /etc/supervisor/conf.d/api-service.conf > /dev/null << 'EOF'
[program:api-service]
command=python3 /opt/test-apps/api_service.py
directory=/opt/test-apps
user=devops
autostart=true
autorestart=false
startsecs=3
startretries=3
exitcodes=0
stopwaitsecs=10
stdout_logfile=/var/log/supervisor/api-service.out.log
stdout_logfile_maxbytes=10MB
stderr_logfile=/var/log/supervisor/api-service.err.log
stderr_logfile_maxbytes=10MB
environment=PYTHONUNBUFFERED="1"
EOF

# 4. Start
echo "[4/4] Start api-service..."
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start api-service 2>/dev/null || true

sleep 3
echo ""
supervisorctl status api-service

# Test health
echo ""
echo "Test health check:"
curl -s http://localhost:8888/health 2>/dev/null && echo "" || echo "(chua san sang)"

echo ""
echo "============================================"
echo " Setup hoan tat!"
echo "============================================"
echo ""
echo "api-service dang RUNNING tren port 8888"
echo ""
echo "De test, quay lai Agent Server chay:"
echo "  bash scripts/test_real_scenario.sh"
