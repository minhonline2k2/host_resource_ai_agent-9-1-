"""Supervisor evidence collector: SSH commands to gather supervisor process data."""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)


# === Supervisor-specific SSH commands ===

SUPERVISOR_BASE_COMMANDS: list[tuple[str, str, str]] = [
    ("sup_status_all", "supervisorctl status", "supervisor_status"),
    ("sup_syslog_oom", "grep -i 'oom\\|killed\\|signal' /var/log/syslog 2>/dev/null | tail -20 || journalctl -k --no-pager 2>/dev/null | grep -i 'oom\\|killed\\|signal' | tail -20 || true", "supervisor_syslog"),
    ("sup_mem_free", "free -m | awk 'NR==2{print $4}'", "system_info"),
    ("sup_mem_detail", "free -m", "system_info"),
    ("sup_disk_usage", "df -h / | awk 'NR==2{print $5}'", "system_info"),
    ("sup_disk_detail", "df -h", "system_info"),
    ("sup_supervisord_log", "tail -80 /var/log/supervisor/supervisord.log 2>/dev/null || true", "supervisor_log"),
    ("sup_uptime_load", "uptime", "system_info"),
]


def build_supervisor_command_pack(
    process_name: str,
    stderr_log_path: str = "",
    stdout_log_path: str = "",
) -> list[tuple[str, str, str]]:
    """Build SSH command pack for supervisor incident.
    
    Phase 1: Get config → discover log paths
    Phase 2: Collect logs + system state
    """
    commands = list(SUPERVISOR_BASE_COMMANDS)

    # Process-specific status
    commands.append((
        "sup_status_process",
        f"supervisorctl status {process_name}",
        "supervisor_status",
    ))

    # Supervisor config — try multiple locations
    commands.append((
        "sup_config",
        f"cat /etc/supervisor/conf.d/{process_name}.conf 2>/dev/null || "
        f"cat /etc/supervisor/conf.d/{process_name}.ini 2>/dev/null || "
        f"cat /etc/supervisord.d/{process_name}.conf 2>/dev/null || "
        f"cat /etc/supervisord.d/{process_name}.ini 2>/dev/null || "
        f"grep -A 20 '\\[program:{process_name}\\]' /etc/supervisor/supervisord.conf 2>/dev/null || "
        f"echo 'CONFIG_NOT_FOUND'",
        "supervisor_config",
    ))

    # Stderr log (150 lines — day du cho LLM phan tich exception/traceback)
    if stderr_log_path:
        commands.append(("sup_stderr", f"tail -n 150 {stderr_log_path}", "supervisor_stderr"))
    else:
        # Try default paths
        commands.append((
            "sup_stderr",
            f"tail -n 150 /var/log/supervisor/{process_name}/{process_name}.err.log 2>/dev/null || "
            f"tail -n 150 /var/log/supervisor/{process_name}.err.log 2>/dev/null || "
            f"tail -n 150 /var/log/supervisor/{process_name}-stderr.log 2>/dev/null || "
            f"echo 'STDERR_LOG_NOT_FOUND'",
            "supervisor_stderr",
        ))

    # Stdout log (80 lines — lien quan den app logic truoc khi crash)
    if stdout_log_path:
        commands.append(("sup_stdout", f"tail -n 80 {stdout_log_path}", "supervisor_stdout"))
    else:
        commands.append((
            "sup_stdout",
            f"tail -n 80 /var/log/supervisor/{process_name}/{process_name}.out.log 2>/dev/null || "
            f"tail -n 80 /var/log/supervisor/{process_name}.out.log 2>/dev/null || "
            f"tail -n 80 /var/log/supervisor/{process_name}-stdout.log 2>/dev/null || "
            f"echo 'STDOUT_LOG_NOT_FOUND'",
            "supervisor_stdout",
        ))

    # Recent restarts in supervisord log
    commands.append((
        "sup_recent_restarts",
        f"grep -i '{process_name}' /var/log/supervisor/supervisord.log 2>/dev/null | tail -30 || true",
        "supervisor_log",
    ))

    # List directories mentioned in recent stderr (find missing files + backups)
    # Extract paths from FileNotFoundError/IOError/Permission denied, then ls -la each dir
    commands.append((
        "sup_referenced_paths",
        f"STDERR=$(tail -n 200 /var/log/supervisor/{process_name}/{process_name}.err.log 2>/dev/null || "
        f"tail -n 200 /var/log/supervisor/{process_name}.err.log 2>/dev/null || "
        f"tail -n 200 /var/log/supervisor/{process_name}-stderr.log 2>/dev/null); "
        f"echo \"$STDERR\" | grep -oE \"'/[^']*'|\\\"/[^\\\"]*\\\"|/[a-zA-Z0-9_./-]+\\.(json|yaml|yml|conf|ini|cfg|env|toml|py|sh|log|db|sock|pid)\" "
        f"| tr -d \"'\\\"\" | sort -u | head -10 | while read P; do "
        f"DIR=$(dirname \"$P\"); "
        f"echo \"=== Referenced path: $P ===\"; "
        f"if [ -e \"$P\" ]; then echo \"EXISTS: $(ls -la \"$P\" 2>/dev/null)\"; else echo \"NOT FOUND: $P\"; fi; "
        f"echo \"--- Directory listing: $DIR ---\"; "
        f"ls -la \"$DIR\" 2>/dev/null | head -30 || echo \"(directory not accessible)\"; "
        f"done",
        "supervisor_paths",
    ))

    # Working directory contents from supervisor config
    commands.append((
        "sup_workdir_files",
        f"DIR=$(grep -A 20 '\\[program:{process_name}\\]' /etc/supervisor/conf.d/*.conf /etc/supervisor/conf.d/*.ini /etc/supervisor/supervisord.conf 2>/dev/null "
        f"| grep -oE 'directory\\s*=\\s*[^ ]+' | head -1 | cut -d'=' -f2 | tr -d ' '); "
        f"if [ -n \"$DIR\" ] && [ -d \"$DIR\" ]; then "
        f"echo \"=== Working directory: $DIR ===\"; "
        f"ls -la \"$DIR\" 2>/dev/null | head -50; "
        f"echo \"--- Backup files (*.bak, *.orig, *.old) ---\"; "
        f"find \"$DIR\" -maxdepth 2 \\( -name '*.bak' -o -name '*.orig' -o -name '*.old' -o -name '*~' \\) 2>/dev/null | head -20; "
        f"else echo 'No working directory in supervisor config'; fi",
        "supervisor_workdir",
    ))

    # Process-specific: neu process dang RUNNING, lay them PID info
    commands.append((
        "sup_proc_detail",
        f"PID=$(supervisorctl pid {process_name} 2>/dev/null); "
        f"if [ \"$PID\" != \"0\" ] && [ -n \"$PID\" ] && [ -d /proc/$PID ]; then "
        f"echo '=== PROCESS INFO ==='; "
        f"echo \"PID: $PID\"; "
        f"echo \"CMD: $(cat /proc/$PID/cmdline 2>/dev/null | tr '\\0' ' ')\"; "
        f"echo \"CWD: $(readlink -f /proc/$PID/cwd 2>/dev/null)\"; "
        f"echo \"RSS_KB: $(awk '/VmRSS/{{print $2}}' /proc/$PID/status 2>/dev/null)\"; "
        f"echo \"THREADS: $(awk '/Threads/{{print $2}}' /proc/$PID/status 2>/dev/null)\"; "
        f"echo \"FD_COUNT: $(ls /proc/$PID/fd 2>/dev/null | wc -l)\"; "
        f"echo \"OPEN_FILES:\"; ls -la /proc/$PID/fd 2>/dev/null | tail -20; "
        f"else echo 'Process not running (PID=0 or no /proc entry)'; fi",
        "supervisor_proc_detail",
    ))

    # Environment variables cua process (giup detect CONFIG_ERR)
    commands.append((
        "sup_proc_env",
        f"PID=$(supervisorctl pid {process_name} 2>/dev/null); "
        f"if [ \"$PID\" != \"0\" ] && [ -n \"$PID\" ] && [ -f /proc/$PID/environ ]; then "
        f"cat /proc/$PID/environ 2>/dev/null | tr '\\0' '\\n' | grep -v -i 'password\\|secret\\|key\\|token' | head -30; "
        f"else echo 'Process not running, cannot read environ'; fi",
        "supervisor_env",
    ))

    # Lich su restart gan day (chi tiet hon)
    commands.append((
        "sup_restart_history",
        f"grep -E '(entered|exited|stopped|starting|FATAL|BACKOFF).*{process_name}' "
        f"/var/log/supervisor/supervisord.log 2>/dev/null | tail -40 || true",
        "supervisor_log",
    ))

    # System context
    commands.extend([
        ("sup_top_mem", "ps -eo pid,user,%cpu,%mem,rss,comm --sort=-rss | head -15", "system_info"),
        ("sup_top_cpu", "ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu | head -10", "system_info"),
        ("sup_dmesg_recent", "dmesg -T 2>/dev/null | tail -40 || true", "system_info"),
        # Network connections (giup detect DEP_FAIL)
        ("sup_network", "ss -tlnp 2>/dev/null | head -20 || true", "system_info"),
        # Systemd journal cho process (neu co)
        ("sup_journal",
         f"journalctl -u supervisor --no-pager -n 30 2>/dev/null || "
         f"journalctl --no-pager -n 20 2>/dev/null | grep -i '{process_name}' || true",
         "system_info"),
    ])

    return commands


def parse_supervisor_status(raw_text: str) -> dict:
    """Parse 'supervisorctl status' output into structured data."""
    result = {
        "processes": [],
        "raw": raw_text,
    }
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        proc = {
            "name": parts[0],
            "state": parts[1],
            "pid": 0,
            "uptime": "",
            "exit_code": None,
        }

        rest = " ".join(parts[2:])
        # Extract PID
        import re
        pid_match = re.search(r'pid (\d+)', rest)
        if pid_match:
            proc["pid"] = int(pid_match.group(1))

        # Extract uptime
        uptime_match = re.search(r'uptime ([\d:]+)', rest)
        if uptime_match:
            proc["uptime"] = uptime_match.group(1)

        # Extract exit status
        exit_match = re.search(r'exit status (\d+)', rest)
        if exit_match:
            proc["exit_code"] = int(exit_match.group(1))

        result["processes"].append(proc)

    return result


def parse_supervisor_config(raw_text: str) -> dict:
    """Parse supervisor config file to extract log paths and settings."""
    config = {
        "stderr_logfile": "",
        "stdout_logfile": "",
        "command": "",
        "directory": "",
        "user": "",
        "autostart": True,
        "autorestart": True,
        "startsecs": 1,
        "startretries": 3,
    }

    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if "=" not in line or line.startswith("[") or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if key == "stderr_logfile":
            config["stderr_logfile"] = value
        elif key == "stdout_logfile":
            config["stdout_logfile"] = value
        elif key == "command":
            config["command"] = value
        elif key == "directory":
            config["directory"] = value
        elif key == "user":
            config["user"] = value
        elif key == "autostart":
            config["autostart"] = value.lower() == "true"
        elif key == "autorestart":
            config["autorestart"] = value.lower() in ("true", "unexpected")
        elif key == "startsecs":
            try:
                config["startsecs"] = int(value)
            except ValueError:
                pass
        elif key == "startretries":
            try:
                config["startretries"] = int(value)
            except ValueError:
                pass

    return config
