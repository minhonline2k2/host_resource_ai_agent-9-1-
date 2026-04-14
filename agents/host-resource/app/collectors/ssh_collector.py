"""SSH evidence collector: runs command packs on remote hosts.

Uses a single shell script approach to avoid connection drops between commands.
Each command is wrapped with markers so output can be split reliably.
"""

from __future__ import annotations

import time
import re
from typing import Optional

import paramiko

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# Marker used to split command outputs
CMD_SEPARATOR = "===AGENT_CMD_BOUNDARY==="


class SSHCollector:
    """Collect evidence from remote host via SSH."""

    def __init__(self, host: str, user: str = None, key_path: str = None):
        self.host = host
        self.user = user or settings.ssh_user
        self.key_path = key_path or settings.ssh_key_path
        self.timeout = settings.ssh_timeout
        self.cmd_timeout = settings.ssh_command_timeout

    def _connect(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = dict(
            hostname=self.host,
            username=self.user,
            timeout=self.timeout,
            banner_timeout=30,
            auth_timeout=30,
        )
        try:
            client.connect(key_filename=self.key_path, **connect_kwargs)
        except Exception:
            client.connect(allow_agent=True, look_for_keys=True, **connect_kwargs)
        # Keep alive to prevent drops
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(15)
        return client

    def run_command(self, command: str) -> dict:
        """Run a single command."""
        start = time.time()
        try:
            client = self._connect()
            _, stdout, stderr = client.exec_command(command, timeout=self.cmd_timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
            client.close()
            return {
                "command": command, "stdout": out, "stderr": err,
                "exit_code": exit_code, "duration_ms": int((time.time() - start) * 1000),
            }
        except Exception as e:
            return {
                "command": command, "stdout": "", "stderr": str(e),
                "exit_code": -1, "duration_ms": int((time.time() - start) * 1000),
            }

    def run_command_pack(self, commands: list[tuple[str, str, str]]) -> list[dict]:
        """Run command pack by batching into a single shell script.
        
        This avoids the 'EOF in transport thread' issue where paramiko drops
        the connection between individual exec_command calls.
        """
        if not commands:
            return []

        # Build a single shell script with markers between commands
        script_lines = ["#!/bin/bash", "set +e"]  # don't exit on error
        for cmd_id, cmd_text, ev_type in commands:
            script_lines.append(f'echo "{CMD_SEPARATOR} CMD_ID={cmd_id} EXIT_CODE_START"')
            script_lines.append(cmd_text)
            script_lines.append(f'echo ""')
            script_lines.append(f'echo "{CMD_SEPARATOR} CMD_ID={cmd_id} EXIT_CODE=$?"')
        
        full_script = "\n".join(script_lines)
        
        logger.info(f"[SSH] Connecting to {self.host} ({len(commands)} commands in 1 session)...")
        start_total = time.time()
        
        try:
            client = self._connect()
        except Exception as e:
            logger.error(f"[SSH] ❌ Connect failed to {self.host}: {e}")
            return [{"command_id": cid, "command_text": cmd, "evidence_type": et,
                      "raw_text": "", "exit_code": -1, "duration_ms": 0,
                      "source_host": self.host} for cid, cmd, et in commands]

        try:
            # Execute entire script via stdin pipe — avoids quoting issues
            total_timeout = max(self.cmd_timeout * 2, len(commands) * 5)
            stdin, stdout, stderr = client.exec_command(
                "bash -s",
                timeout=total_timeout,
            )
            stdin.write(full_script + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
            raw_output = stdout.read().decode("utf-8", errors="replace")
            raw_stderr = stderr.read().decode("utf-8", errors="replace")
            overall_exit = stdout.channel.recv_exit_status()
        except Exception as e:
            logger.error(f"[SSH] ❌ Script execution failed: {e}")
            # Fallback: run commands one by one with reconnect
            client.close()
            return self._run_commands_individual(commands)
        finally:
            client.close()
        
        total_ms = int((time.time() - start_total) * 1000)
        logger.info(f"[SSH] ✅ Script completed in {total_ms}ms")
        
        # Parse output by splitting on markers
        return self._parse_batch_output(raw_output, commands)

    def _parse_batch_output(self, raw_output: str, commands: list[tuple[str, str, str]]) -> list[dict]:
        """Parse the batched script output back into individual command results."""
        results = []
        cmd_map = {cid: (cmd, et) for cid, cmd, et in commands}
        
        # Split by separator
        sections = raw_output.split(CMD_SEPARATOR)
        
        # Group sections: for each command we get a START marker, output, then EXIT_CODE marker
        current_cmd_id = None
        current_output_lines = []
        exit_codes = {}
        outputs = {}
        
        for section in sections:
            section = section.strip()
            if not section:
                continue
            
            # Check if this is a marker line
            start_match = re.match(r'CMD_ID=(\S+)\s+EXIT_CODE_START', section)
            exit_match = re.match(r'CMD_ID=(\S+)\s+EXIT_CODE=(\d+)', section)
            
            if start_match:
                current_cmd_id = start_match.group(1)
                current_output_lines = []
            elif exit_match:
                cmd_id = exit_match.group(1)
                exit_codes[cmd_id] = int(exit_match.group(2))
                # Remaining text before marker is part of output
                remaining = re.sub(r'CMD_ID=\S+\s+EXIT_CODE=\d+', '', section).strip()
                if current_cmd_id and current_cmd_id == cmd_id:
                    outputs[cmd_id] = "\n".join(current_output_lines)
                    if remaining:
                        outputs[cmd_id] = (outputs.get(cmd_id, "") + "\n" + remaining).strip()
                current_cmd_id = None
            elif current_cmd_id:
                current_output_lines.append(section)
        
        # Build results for each command
        for cmd_id, cmd_text, ev_type in commands:
            output = outputs.get(cmd_id, "")
            exit_code = exit_codes.get(cmd_id, -1)
            results.append({
                "command_id": cmd_id,
                "command_text": cmd_text,
                "evidence_type": ev_type,
                "raw_text": output[:50000],
                "exit_code": exit_code,
                "duration_ms": 0,
                "source_host": self.host,
            })
        
        # If parsing failed to find any results, fallback to putting all output in first command
        if not any(r["raw_text"] for r in results) and raw_output.strip():
            logger.warning(f"[SSH] Marker parsing failed, using raw output fallback")
            results = self._run_commands_individual(commands)
        
        return results

    def _run_commands_individual(self, commands: list[tuple[str, str, str]]) -> list[dict]:
        """Fallback: run each command with its own SSH connection."""
        results = []
        for cmd_id, cmd_text, ev_type in commands:
            start = time.time()
            try:
                client = self._connect()
                _, stdout, stderr = client.exec_command(cmd_text, timeout=self.cmd_timeout)
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                exit_code = stdout.channel.recv_exit_status()
                client.close()
            except Exception as e:
                out, err, exit_code = "", str(e), -1
            results.append({
                "command_id": cmd_id,
                "command_text": cmd_text,
                "evidence_type": ev_type,
                "raw_text": out[:50000],
                "exit_code": exit_code,
                "duration_ms": int((time.time() - start) * 1000),
                "source_host": self.host,
            })
        return results


# === Command Packs ===

BASELINE_COMMANDS: list[tuple[str, str, str]] = [
    ("date", "date", "baseline"),
    ("hostname", "hostname -f", "baseline"),
    ("uptime", "uptime", "baseline"),
    ("loadavg", "cat /proc/loadavg", "baseline"),
    ("free", "free -m", "baseline"),
    ("vmstat", "vmstat 1 3", "baseline"),
    ("df_h", "df -h", "baseline"),
    ("df_ih", "df -ih", "baseline"),
    ("lsblk", "lsblk -o NAME,KNAME,TYPE,SIZE,FSTYPE,MOUNTPOINT", "baseline"),
]

PROCESS_COMMANDS: list[tuple[str, str, str]] = [
    ("top_cpu", "ps -eo pid,ppid,user,state,stat,%cpu,%mem,rss,vsz,etime,comm,args --sort=-%cpu | head -40", "process_cpu"),
    ("top_mem", "ps -eo pid,ppid,user,state,stat,%cpu,%mem,rss,vsz,etime,comm,args --sort=-rss | head -40", "process_mem"),
    ("agg_cmd", """ps -eo comm=,%cpu=,%mem=,rss= | awk '{c[$1]++; cpu[$1]+=$2; mem[$1]+=$3; rss[$1]+=$4} END{for(i in c) printf "%-25s count=%-6d total_cpu=%-10.2f total_mem=%-10.2f total_rss_kb=%-12d\\n",i,c[i],cpu[i],mem[i],rss[i]}' | sort -k3 -nr | head -50""", "process_aggregated"),
    ("zombies", "ps -eo pid,ppid,user,state,stat,etime,comm,args --no-headers | awk '$4 ~ /Z/ {print}'", "process_anomalies"),
    ("d_state", "ps -eo pid,ppid,user,state,stat,wchan:32,etime,comm,args --no-headers | awk '$4 ~ /D/ {print}'", "process_anomalies"),
    ("thread_counts", "ps -eLo pid= | sort | uniq -c | sort -nr | head -30", "process_anomalies"),
    ("thread_top", "ps -eLo pid,ppid,tid,pcpu,pmem,stat,comm --sort=-pcpu | head -80", "process_anomalies"),
]

SERVICE_COMMANDS: list[tuple[str, str, str]] = [
    ("supervisor", "supervisorctl status 2>/dev/null || true", "service_state"),
    ("systemd_failed", "systemctl --failed --no-pager 2>/dev/null || true", "service_state"),
    ("systemd_active", "systemctl list-units --type=service --state=active --no-pager 2>/dev/null | head -100", "service_state"),
    ("recent_starts", "journalctl --since '30 min ago' --no-pager 2>/dev/null | egrep -i 'Started|Starting|Stopped|Failed' | tail -50 || true", "service_state"),
]

KERNEL_COMMANDS: list[tuple[str, str, str]] = [
    ("dmesg", "dmesg -T 2>/dev/null | tail -200", "kernel_journal"),
    ("dmesg_grep", "dmesg -T 2>/dev/null | egrep -i 'oom|killed process|blocked for more than|segfault|i/o error|ext4|xfs|filesystem|buffer' | tail -50 || true", "kernel_journal"),
    ("journal_err", "journalctl -p err -n 200 --no-pager 2>/dev/null || true", "kernel_journal"),
    ("journal_recent", "journalctl --since '30 min ago' --no-pager 2>/dev/null | tail -300 || true", "kernel_journal"),
]

SOCKET_FD_COMMANDS: list[tuple[str, str, str]] = [
    ("ss_summary", "ss -s", "socket_fd"),
    ("ss_established", "ss -ant state established | awk '{print $4,$5}' | sort | uniq -c | sort -nr | head -30", "socket_fd"),
    ("ss_listen", "ss -lntp | head -100", "socket_fd"),
    ("deleted_open", "lsof +L1 2>/dev/null | head -100 || true", "socket_fd"),
]

CRON_COMMANDS: list[tuple[str, str, str]] = [
    ("crontab", "cat /etc/crontab 2>/dev/null || true", "cron_backup"),
    ("cron_d", "ls /etc/cron.d/ 2>/dev/null && for f in /etc/cron.d/*; do [ -f \"$f\" ] && echo \"### $f\" && cat \"$f\"; done 2>/dev/null || true", "cron_backup"),
    ("timers", "systemctl list-timers --all --no-pager 2>/dev/null | head -50", "cron_backup"),
    ("recent_modified", "find /var/log /tmp /var/tmp -type f -mmin -60 -printf '%TY-%Tm-%Td %TH:%TM\\t%s\\t%p\\n' 2>/dev/null | sort -k2 -nr | head -30 || true", "cron_backup"),
]

CPU_COMMANDS: list[tuple[str, str, str]] = [
    ("top_snap", "top -bn1 -o %CPU | head -30", "cpu_detail"),
    ("mpstat", "mpstat -P ALL 1 3 2>/dev/null || true", "cpu_detail"),
    ("pidstat_cpu", "pidstat -u 1 3 2>/dev/null | tail -50 || true", "cpu_detail"),
    ("vmstat5", "vmstat 1 5", "cpu_detail"),
    ("iostat", "iostat -x 1 2 2>/dev/null | tail -30 || true", "cpu_detail"),
]

RAM_COMMANDS: list[tuple[str, str, str]] = [
    ("meminfo", "grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|Dirty|Writeback|Slab' /proc/meminfo", "memory_detail"),
    ("swapon", "swapon --show 2>/dev/null || true", "memory_detail"),
    ("vmstat_swap", "vmstat 1 5", "memory_detail"),
    ("oom_logs", "dmesg -T 2>/dev/null | egrep -i 'out of memory|oom|killed process' | tail -20 || true", "memory_detail"),
    ("thp", "cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null; grep -E 'Dirty|Writeback' /proc/meminfo || true", "memory_detail"),
]

DISK_COMMANDS: list[tuple[str, str, str]] = [
    ("df_detail", "df -PTh 2>/dev/null | grep -v tmpfs | grep -v overlay | grep -v devtmpfs", "disk_detail"),
    ("inode_detail", "df -Pi 2>/dev/null | grep -v tmpfs | grep -v overlay", "disk_detail"),
    ("du_top", "du -x -sh /var/log /tmp /var/tmp /home /opt /data /backup 2>/dev/null | sort -rh | head -20", "disk_detail"),
    ("large_files", "find /var/log /tmp /var/tmp /home /opt /data /backup -type f -size +100M -printf '%s\\t%p\\n' 2>/dev/null | sort -rn | head -30 || true", "disk_detail"),
    ("recent_files", "find /var/log /tmp /var/tmp -type f -mmin -60 -printf '%s\\t%p\\n' 2>/dev/null | sort -rn | head -20 || true", "disk_detail"),
    ("deleted_disk", "lsof +L1 2>/dev/null | awk 'NR>1 {print $2,$4,$7,$9}' | head -50 || true", "disk_detail"),
    ("docker_df", "docker system df 2>/dev/null || true", "disk_detail"),
    ("iostat_disk", "iostat -x 1 2 2>/dev/null | tail -30 || true", "disk_detail"),
]

ROLE_APP_COMMANDS: list[tuple[str, str, str]] = [
    ("app_procs", "ps -ef | egrep 'java|tomcat|php-fpm|nginx' | grep -v grep | head -100", "role_hints"),
    ("app_config", "grep -RniE 'max_children|pm.max_children|worker_processes|worker_connections' /etc/php /etc/nginx 2>/dev/null | head -50 || true", "role_hints"),
    ("app_logs", "find /var/log /opt -type f -name '*.log' -printf '%s\\t%p\\n' 2>/dev/null | sort -rn | head -30", "role_hints"),
]

ROLE_BATCH_COMMANDS: list[tuple[str, str, str]] = [
    ("redis_status", "systemctl status redis-server --no-pager 2>/dev/null | head -30 || true", "role_hints"),
    ("redis_info", "redis-cli INFO 2>/dev/null | egrep 'used_memory_human|maxmemory_human|mem_fragmentation_ratio|evicted_keys|blocked_clients|connected_clients' || true", "role_hints"),
    ("kafka_status", "systemctl status kafka --no-pager 2>/dev/null | head -30 || true", "role_hints"),
]

ROLE_DB_COMMANDS: list[tuple[str, str, str]] = [
    ("mariadb_status", "systemctl status mariadb --no-pager 2>/dev/null | head -30 || true", "role_hints"),
    ("mysql_threads", "mysql -e \"SHOW GLOBAL STATUS LIKE 'Threads_running'; SHOW GLOBAL STATUS LIKE 'Threads_connected';\" 2>/dev/null || true", "role_hints"),
    ("pg_status", "systemctl status postgresql --no-pager 2>/dev/null | head -30 || true", "role_hints"),
]

ROLE_PROXY_COMMANDS: list[tuple[str, str, str]] = [
    ("nginx_test", "nginx -t 2>&1 || true", "role_hints"),
    ("nginx_err", "tail -200 /var/log/nginx/error.log 2>/dev/null || true", "role_hints"),
]

ROLE_JENKINS_COMMANDS: list[tuple[str, str, str]] = [
    ("jenkins_disk", "du -x -sh /home/jenkins /var/lib/jenkins /opt/tomcat/backup 2>/dev/null | sort -rh || true", "role_hints"),
    ("jenkins_procs", "ps -ef | egrep 'jenkins|java|rsync|tar|gzip' | grep -v grep || true", "role_hints"),
]


def build_command_pack(resource_type: str, host_role: str) -> list[tuple[str, str, str]]:
    """Build the full command pack based on resource type and host role."""
    pack = list(BASELINE_COMMANDS)
    pack.extend(PROCESS_COMMANDS)
    pack.extend(SERVICE_COMMANDS)
    pack.extend(KERNEL_COMMANDS)
    pack.extend(SOCKET_FD_COMMANDS)
    pack.extend(CRON_COMMANDS)

    rt = resource_type.upper()
    if rt == "CPU":
        pack.extend(CPU_COMMANDS)
    elif rt == "RAM":
        pack.extend(RAM_COMMANDS)
    elif rt == "DISK":
        pack.extend(DISK_COMMANDS)
    else:
        pack.extend(CPU_COMMANDS)
        pack.extend(RAM_COMMANDS)
        pack.extend(DISK_COMMANDS)

    role = host_role.lower()
    if role == "app":
        pack.extend(ROLE_APP_COMMANDS)
    elif role == "batch":
        pack.extend(ROLE_BATCH_COMMANDS)
    elif role == "db":
        pack.extend(ROLE_DB_COMMANDS)
    elif role == "proxy":
        pack.extend(ROLE_PROXY_COMMANDS)
    elif role == "jenkins":
        pack.extend(ROLE_JENKINS_COMMANDS)

    return pack
