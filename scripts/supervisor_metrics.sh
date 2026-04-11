#!/bin/bash
# =================================================================
# Supervisor Metrics Exporter for Node Exporter (textfile collector)
# Cài trên Server 2 — chạy mỗi 15s qua cron hoặc systemd timer
# Output: /var/lib/node_exporter/textfile_collector/supervisor.prom
# =================================================================

OUTPUT_DIR="/var/lib/node_exporter/textfile_collector"
OUTPUT_FILE="${OUTPUT_DIR}/supervisor.prom"
TMP_FILE="${OUTPUT_FILE}.tmp"

mkdir -p "$OUTPUT_DIR"

# Header
cat > "$TMP_FILE" <<'HEADER'
# HELP supervisor_process_state Process state: 0=STOPPED 1=RUNNING 2=FATAL 3=BACKOFF 4=STARTING 5=STOPPING 6=EXITED 7=UNKNOWN
# TYPE supervisor_process_state gauge
# HELP supervisor_process_uptime_seconds Process uptime in seconds
# TYPE supervisor_process_uptime_seconds gauge
# HELP supervisor_process_exit_code Last known exit code
# TYPE supervisor_process_exit_code gauge
# HELP supervisor_process_pid Process PID (0 if not running)
# TYPE supervisor_process_pid gauge
HEADER

# Parse supervisorctl status output
supervisorctl status 2>/dev/null | while IFS= read -r line; do
    # Example: demo_supervisor_service    RUNNING   pid 10546, uptime 0:00:30
    # Example: celery-worker              FATAL     Exited too quickly (process log may have details)
    # Example: redis-cache                STOPPED   Apr 10 07:06 AM

    # Extract process name (first field)
    PROC_NAME=$(echo "$line" | awk '{print $1}')
    [ -z "$PROC_NAME" ] && continue

    # Extract state (second field)
    STATE_STR=$(echo "$line" | awk '{print $2}')

    # Map state to number
    case "$STATE_STR" in
        STOPPED)  STATE=0 ;;
        RUNNING)  STATE=1 ;;
        FATAL)    STATE=2 ;;
        BACKOFF)  STATE=3 ;;
        STARTING) STATE=4 ;;
        STOPPING) STATE=5 ;;
        EXITED)   STATE=6 ;;
        *)        STATE=7 ;;
    esac

    # Extract PID (if RUNNING)
    PID=0
    if [ "$STATE" -eq 1 ]; then
        PID=$(echo "$line" | grep -oP 'pid \K[0-9]+' || echo 0)
    fi

    # Extract uptime (if RUNNING) — format: H:MM:SS or D days, H:MM:SS
    UPTIME_SEC=0
    if [ "$STATE" -eq 1 ]; then
        UPTIME_RAW=$(echo "$line" | grep -oP 'uptime \K[\d:]+' || echo "")
        if [ -n "$UPTIME_RAW" ]; then
            IFS=':' read -ra T <<< "$UPTIME_RAW"
            case ${#T[@]} in
                3) UPTIME_SEC=$(( ${T[0]#0}*3600 + ${T[1]#0}*60 + ${T[2]#0} )) ;;
                2) UPTIME_SEC=$(( ${T[0]#0}*60 + ${T[1]#0} )) ;;
                1) UPTIME_SEC=${T[0]#0} ;;
            esac
        fi
        # Check for days
        DAYS=$(echo "$line" | grep -oP '(\d+) days' | grep -oP '\d+' || echo 0)
        UPTIME_SEC=$(( UPTIME_SEC + DAYS * 86400 ))
    fi

    # Extract exit code from supervisor if EXITED or FATAL
    EXIT_CODE=0
    if [ "$STATE" -eq 2 ] || [ "$STATE" -eq 6 ]; then
        # Try to get from supervisorctl status output
        EXIT_CODE=$(echo "$line" | grep -oP 'exit status \K[0-9]+' || echo 0)
        [ -z "$EXIT_CODE" ] && EXIT_CODE=0
    fi

    # Write metrics
    echo "supervisor_process_state{name=\"${PROC_NAME}\"} ${STATE}" >> "$TMP_FILE"
    echo "supervisor_process_uptime_seconds{name=\"${PROC_NAME}\"} ${UPTIME_SEC}" >> "$TMP_FILE"
    echo "supervisor_process_exit_code{name=\"${PROC_NAME}\"} ${EXIT_CODE}" >> "$TMP_FILE"
    echo "supervisor_process_pid{name=\"${PROC_NAME}\"} ${PID}" >> "$TMP_FILE"

done

# Atomic move
mv "$TMP_FILE" "$OUTPUT_FILE"
