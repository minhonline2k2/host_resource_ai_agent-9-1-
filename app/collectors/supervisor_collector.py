"""Supervisor evidence collector: SSH commands to gather supervisor process data."""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)


# === Supervisor-specific SSH commands ===

SUPERVISOR_BASE_COMMANDS: list[tuple[str, str, str]] = [
    ("sup_status_all", "supervisorctl status", "supervisor_status"),
    ("sup_syslog_oom", "grep -i 'oom\\|killed\\|signal' /var/log/syslog 2>/dev/null | tail -20 || journalctl -k --no-pager 2>/dev/null | grep -i 'oom\\|killed\\|signal' | tail -20 || true", "supervisor_syslog"),
    ("sup_mem_free", "free -m | awk 'NR==2{print $4}'", "system_info"),
    ("sup_disk_usage", "df -h / | awk 'NR==2{print $5}'", "system_info"),
    ("sup_supervisord_log", "tail -50 /var/log/supervisor/supervisord.log 2>/dev/null || true", "supervisor_log"),
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

    # Stderr log (80 lines)
    if stderr_log_path:
        commands.append(("sup_stderr", f"tail -n 80 {stderr_log_path}", "supervisor_stderr"))
    else:
        # Try default paths
        commands.append((
            "sup_stderr",
            f"tail -n 80 /var/log/supervisor/{process_name}/{process_name}.err.log 2>/dev/null || "
            f"tail -n 80 /var/log/supervisor/{process_name}.err.log 2>/dev/null || "
            f"tail -n 80 /var/log/supervisor/{process_name}-stderr.log 2>/dev/null || "
            f"echo 'STDERR_LOG_NOT_FOUND'",
            "supervisor_stderr",
        ))

    # Stdout log (40 lines)
    if stdout_log_path:
        commands.append(("sup_stdout", f"tail -n 40 {stdout_log_path}", "supervisor_stdout"))
    else:
        commands.append((
            "sup_stdout",
            f"tail -n 40 /var/log/supervisor/{process_name}/{process_name}.out.log 2>/dev/null || "
            f"tail -n 40 /var/log/supervisor/{process_name}.out.log 2>/dev/null || "
            f"tail -n 40 /var/log/supervisor/{process_name}-stdout.log 2>/dev/null || "
            f"echo 'STDOUT_LOG_NOT_FOUND'",
            "supervisor_stdout",
        ))

    # Recent restarts in supervisord log
    commands.append((
        "sup_recent_restarts",
        f"grep -i '{process_name}' /var/log/supervisor/supervisord.log 2>/dev/null | tail -30 || true",
        "supervisor_log",
    ))

    # System context
    commands.extend([
        ("sup_top_mem", "ps -eo pid,user,%cpu,%mem,rss,comm --sort=-rss | head -15", "system_info"),
        ("sup_dmesg_recent", "dmesg -T 2>/dev/null | tail -30 || true", "system_info"),
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
