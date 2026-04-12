#!/bin/bash
# =============================================================================
# Setup test supervisor apps on TARGET SERVER (172.16.50.147)
# Tao 3 app gia de test cac loai loi supervisor khac nhau
#
# Usage: bash setup_test_supervisor.sh
# =============================================================================

set -e

echo "============================================"
echo " Setup Test Supervisor Apps"
echo "============================================"

# 1. Cai supervisor neu chua co
if ! command -v supervisord &> /dev/null; then
    echo "[1/5] Cai dat supervisor..."
    sudo apt-get update && sudo apt-get install -y supervisor
    sudo systemctl enable supervisor
    sudo systemctl start supervisor
else
    echo "[1/5] Supervisor da co."
fi

# 2. Tao thu muc
echo "[2/5] Tao thu muc..."
sudo mkdir -p /opt/test-apps
sudo mkdir -p /var/log/supervisor
sudo chown -R devops:devops /opt/test-apps

# 3. Tao app 1: demo-worker (app binh thuong, co the trigger nhieu loai loi)
echo "[3/5] Tao demo-worker app..."
cat > /opt/test-apps/demo_worker.py << 'PYEOF'
#!/usr/bin/env python3
"""Demo worker - simulates a real background worker.
Trigger loi bang cach tao file flag:
  touch /tmp/demo-worker-crash        -> crash voi exception (CODE_ERR)
  touch /tmp/demo-worker-oom          -> eat memory cho den OOM (OOM)
  touch /tmp/demo-worker-depfail      -> connection refused (DEP_FAIL)
  touch /tmp/demo-worker-configerr    -> config error (CONFIG_ERR)
  touch /tmp/demo-worker-permfail     -> permission denied (PERM_ERR)
"""
import os
import sys
import time
import signal
import json

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def main():
    log("demo-worker starting...")
    log(f"PID={os.getpid()}, Python={sys.version}")

    # Check config file
    config_path = os.environ.get("WORKER_CONFIG", "/opt/test-apps/worker_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
        log(f"Config loaded: {config}")
    else:
        log(f"Config file not found: {config_path}")

    cycle = 0
    while True:
        cycle += 1

        # Check crash flags
        if os.path.exists("/tmp/demo-worker-crash"):
            os.remove("/tmp/demo-worker-crash")
            log("ERROR: Unhandled exception triggered!")
            raise RuntimeError("NullPointerException in TaskProcessor.process() at line 142: task_result.data was None")

        if os.path.exists("/tmp/demo-worker-oom"):
            os.remove("/tmp/demo-worker-oom")
            log("WARNING: Memory allocation starting...")
            data = []
            while True:
                data.append("x" * (1024 * 1024))  # 1MB per iteration
                if len(data) % 100 == 0:
                    log(f"Allocated {len(data)} MB...")

        if os.path.exists("/tmp/demo-worker-depfail"):
            os.remove("/tmp/demo-worker-depfail")
            log("Connecting to Redis at 10.0.0.99:6379...")
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(("10.0.0.99", 6379))  # Will fail: connection refused

        if os.path.exists("/tmp/demo-worker-configerr"):
            os.remove("/tmp/demo-worker-configerr")
            log("Loading config...")
            db_url = os.environ["DATABASE_URL"]  # Will crash: KeyError

        if os.path.exists("/tmp/demo-worker-permfail"):
            os.remove("/tmp/demo-worker-permfail")
            log("Opening data file...")
            with open("/etc/shadow", "r") as f:  # Will fail: permission denied
                f.read()

        # Normal operation
        if cycle % 30 == 0:
            log(f"Heartbeat: cycle={cycle}, status=OK")

        time.sleep(1)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda s, f: (log("Received SIGTERM, shutting down..."), sys.exit(0)))
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted, exiting.")
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
PYEOF

# 4. Tao config file
echo "[4/5] Tao config files..."
cat > /opt/test-apps/worker_config.json << 'EOF'
{
    "queue_name": "tasks",
    "batch_size": 10,
    "retry_max": 3,
    "log_level": "INFO"
}
EOF

# 5. Tao supervisor config
echo "[5/5] Tao supervisor configs..."

sudo tee /etc/supervisor/conf.d/demo-worker.conf > /dev/null << 'EOF'
[program:demo-worker]
command=python3 /opt/test-apps/demo_worker.py
directory=/opt/test-apps
user=devops
autostart=true
autorestart=unexpected
startsecs=3
startretries=3
exitcodes=0
stopwaitsecs=10
stdout_logfile=/var/log/supervisor/demo-worker.out.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
stderr_logfile=/var/log/supervisor/demo-worker.err.log
stderr_logfile_maxbytes=10MB
stderr_logfile_backups=3
environment=WORKER_CONFIG="/opt/test-apps/worker_config.json",PYTHONUNBUFFERED="1"
EOF

# Reload supervisor
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start demo-worker || true

echo ""
echo "============================================"
echo " Setup hoan tat!"
echo "============================================"
echo ""
echo "Kiem tra status:"
echo "  supervisorctl status"
echo ""
echo "Trigger loi de test:"
echo "  touch /tmp/demo-worker-crash      # CODE_ERR: exception"
echo "  touch /tmp/demo-worker-oom        # OOM: eat memory"
echo "  touch /tmp/demo-worker-depfail    # DEP_FAIL: connection refused"
echo "  touch /tmp/demo-worker-configerr  # CONFIG_ERR: missing env var"
echo "  touch /tmp/demo-worker-permfail   # PERM_ERR: permission denied"
echo ""
echo "Xem log:"
echo "  tail -f /var/log/supervisor/demo-worker.err.log"
echo "  tail -f /var/log/supervisor/demo-worker.out.log"
echo "  tail -f /var/log/supervisor/supervisord.log"
