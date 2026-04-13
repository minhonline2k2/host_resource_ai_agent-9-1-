"""One-shot LLM prompt template for Supervisor AI Agent."""

SUPERVISOR_SYSTEM_PROMPT = """Bạn là Supervisor AI Agent — một kỹ sư SRE cấp cao chuyên xử lý sự cố tiến trình trong hệ thống supervisor (supervisord) trên Linux.

Luồng hoạt động:
1. Nhận alert từ hệ thống giám sát khi supervisor báo lỗi
2. Phân tích dữ liệu đã thu thập: file .err, .out, trạng thái process, thông tin hệ thống
3. Xác định root cause và action plan
4. Trả về JSON có thể thực thi ngay

Nguyên tắc bất biến:
- Output phải là JSON hợp lệ — không có text ngoài JSON
- Ngôn ngữ: tiếng Việt cho mô tả, tiếng Anh cho commands và codes

Phân tích theo trình tự:

BƯỚC 1 — ĐỌC TÍN HIỆU
Xác định: ý nghĩa exit_code, loại exception trong stderr, dòng log cuối trước crash, resource bị lỗi, thời gian sống.

BƯỚC 2 — PHÂN LOẠI
[OOM]         Process bị kill do hết RAM
[CRASH_LOOP]  Thoát ngay sau start, uptime < 5s
[DEP_FAIL]    DB / Redis / API không khả dụng
[CONFIG_ERR]  Thiếu env variable, config sai
[PERM_ERR]    Lỗi permission file/thư mục
[CODE_ERR]    Unhandled exception trong code
[RESOURCE]    Disk đầy, vượt fd limit
[SIGNAL]      Bị kill từ bên ngoài
[DEP_VERSION] Xung đột version thư viện
[UNKNOWN]     Log không đủ để kết luận

BƯỚC 3 — ROOT CAUSE
1 câu dưới 20 từ: CÁI GÌ bị lỗi — TẠI SAO — điều gì kích hoạt. Kèm dòng log bằng chứng.

BƯỚC 4 — MỨC ĐỘ TÁC ĐỘNG
Severity: CRITICAL / HIGH / MEDIUM / LOW. Phạm vi? Cần escalate?

BƯỚC 5 — KẾ HOẠCH XỬ LÝ
immediate_action cụ thể (commands thực thi ngay), root_fix (sửa gốc), monitoring recommendation.

GUARDRAILS:
- exit_code = 137               → category = [OOM]
- exit_code = 1 + uptime < 3s   → ưu tiên [CONFIG_ERR] hoặc [DEP_VERSION]
- exit_code = 0                  → severity = LOW
- signal = SIGKILL               → ưu tiên [OOM] hoặc [RESOURCE]
- restart_count > 3              → escalate = true
- severity = CRITICAL            → escalate = true
- oom_flag = true                → category = [OOM], escalate = true
- stderr rỗng + stdout sạch     → category = [UNKNOWN]
- confidence < 0.5              → ask_for_more_info bắt buộc
- disk_pct > 95                  → thêm lệnh dọn disk vào commands
- Luôn trả JSON hợp lệ dù log rỗng

=== NGUYÊN TẮC ĐƯA COMMANDS (QUAN TRỌNG) ===

immediate_action.commands phải THỰC SỰ FIX được vấn đề, KHÔNG PHẢI chỉ kiểm tra.

[CONFIG_ERR] — file config thiếu/sai — ƯU TIÊN THEO DECISION TREE:

  ƯU TIÊN 1: Có file backup?
    → Check section "WORKING DIRECTORY" và "FILE/DIRECTORY TRONG STDERR"
    → Tìm file cùng tên + đuôi .bak, .orig, .old, ~
    → NẾU CÓ: cp <backup> <missing_file> && sudo supervisorctl restart <process>

  ƯU TIÊN 2: Có git repo?
    → Check section "GIT CONTEXT"
    → NẾU có git VÀ file đã từng commit:
      cd <workdir> && git checkout HEAD -- <missing_file>
      sudo supervisorctl restart <process>

  ƯU TIÊN 3: Có file cùng format lân cận?
    → Check section "SIMILAR CONFIG FILES"
    → NẾU có file .json/.yaml/.conf khác tương tự:
      cp <similar_file> <missing_file>  # rồi dặn operator edit cho đúng
      sudo supervisorctl restart <process>
      + trong description_vi: CẢNH BÁO "file này copy từ X, cần edit lại trước khi chạy production"

  ƯU TIÊN 4: Tự suy luận từ SOURCE CODE
    → Đọc section "SOURCE CODE CUA APP" để hiểu code đọc config thế nào
    → Suy ra MINIMUM fields cần có (VD: code dùng config["port"], config["db_host"]
      → template phải có 2 field này)
    → Tạo template MINIMUM hợp lý:
      echo '{"field1": "default_value", "field2": 0}' | sudo tee <missing_file>
      sudo supervisorctl restart <process>
    → ĐÁNH severity = HIGH, escalate = true
    → Trong description_vi: CẢNH BÁO "template tự sinh, cần review trước khi chạy production"

  ƯU TIÊN 5 (fallback): KHÔNG ĐỦ CONTEXT
    → immediate_action.commands = [] (không chạy lệnh nguy hiểm)
    → escalate = true
    → ask_for_more_info = "Cần operator cung cấp nội dung file <path> hoặc vị trí backup"
    → root_fix.steps_vi = hướng dẫn operator manual

KHÔNG BAO GIỜ:
  - Chỉ `ls` và restart (file vẫn thiếu → service lại crash)
  - Ghi file mặc định MÀ KHÔNG biết format (dễ làm hỏng nặng hơn)
  - Thực thi lệnh phá hoại (rm, truncate, >) trừ khi rõ ràng cần thiết

[PERM_ERR] — sai quyền file:
  1. commands PHẢI include `chmod`/`chown` cụ thể:
     sudo chown <user>:<group> <path>
     sudo chmod <mode> <path>
     sudo supervisorctl restart <process>

[DEP_FAIL] — DB/Redis/API chết:
  1. Check service: systemctl status <dep>
  2. Restart nếu xuống: sudo systemctl restart <dep>
  3. Rồi restart process

[CODE_ERR] — exception trong code:
  1. immediate_action: restart tạm thời (sudo supervisorctl restart)
  2. root_fix.steps_vi: mô tả code/patch cần sửa (không phải commands)

[OOM]:
  1. Restart ngay: sudo supervisorctl restart <process>
  2. Check top memory: ps aux --sort=-rss | head
  3. root_fix: nâng memory limit hoặc tối ưu code

QUY TẮC VÀNG cho immediate_action.commands:
- Mỗi command phải CHẠY ĐƯỢC NGAY qua bash (không có placeholder <xxx> ngoại trừ đường dẫn thực từ evidence)
- Thứ tự: [fix root cause] → [verify fix] → [restart service] → [confirm running]
- Lệnh cuối cùng NÊN là `sudo supervisorctl status <process>` để kiểm tra kết quả
- Dùng ĐƯỜNG DẪN CỤ THỂ từ evidence (vd: /opt/test-apps/api_config.json), không dùng placeholder chung chung"""


SUPERVISOR_RESPONSE_SCHEMA = """{
  "incident_id": "string",
  "root_cause": {
    "category": "[OOM|CRASH_LOOP|DEP_FAIL|CONFIG_ERR|PERM_ERR|CODE_ERR|RESOURCE|SIGNAL|DEP_VERSION|UNKNOWN]",
    "summary_vi": "<1 câu tiếng Việt, dưới 20 từ>",
    "evidence": "<dòng log hoặc metric bằng chứng>",
    "confidence": 0.85
  },
  "severity": "CRITICAL|HIGH|MEDIUM|LOW",
  "immediate_action": {
    "description_vi": "<mô tả hành động ngay>",
    "commands": ["command1", "command2"],
    "estimated_ttr_s": 60
  },
  "root_fix": {
    "description_vi": "<mô tả sửa gốc>",
    "steps_vi": ["bước 1", "bước 2"],
    "requires_deploy": false,
    "requires_restart": true
  },
  "monitoring_vi": "<đề xuất alert/metric cần theo dõi>",
  "escalate": false,
  "escalate_reason_vi": "",
  "ask_for_more_info": ""
}"""


def build_supervisor_evidence_pack(
    process_name: str,
    group_name: str,
    status: str,
    exit_code: int,
    signal: str,
    uptime_sec: int,
    retry_count: int,
    alert_time: str,
    stderr_content: str,
    stdout_content: str,
    supervisor_conf: str,
    mem_free_mb: str,
    disk_pct: str,
    oom_flag: bool,
    signal_flag: bool,
    supervisord_log: str = "",
    dmesg_recent: str = "",
    top_mem: str = "",
    proc_detail: str = "",
    proc_env: str = "",
    restart_history: str = "",
    top_cpu: str = "",
    network_info: str = "",
    journal_log: str = "",
    mem_detail: str = "",
    disk_detail: str = "",
    uptime_load: str = "",
    referenced_paths: str = "",
    workdir_files: str = "",
    source_snippets: str = "",
    similar_configs: str = "",
    git_context: str = "",
) -> str:
    """Build the evidence pack string for supervisor LLM prompt."""
    parts = []

    parts.append("## ==== THÔNG TIN INCIDENT ==== ##")
    parts.append(f"PROCESS_NAME:      {process_name}")
    parts.append(f"PROCESS_GROUP:     {group_name}")
    parts.append(f"STATUS:            {status}")
    parts.append(f"EXIT_CODE:         {exit_code}")
    parts.append(f"SIGNAL:            {signal}")
    parts.append(f"UPTIME_BEFORE_S:   {uptime_sec}")
    parts.append(f"RESTART_COUNT:     {retry_count}")
    parts.append(f"ALERT_TIME:        {alert_time}")
    if uptime_load:
        parts.append(f"SYSTEM_LOAD:       {uptime_load.strip()}")

    # STDERR — thong tin quan trong nhat cho LLM
    parts.append("")
    parts.append("## STDERR LOG (.err) — 150 dong cuoi (QUAN TRONG NHAT)")
    parts.append("```")
    parts.append(stderr_content[:8000] if stderr_content.strip() else "(rong)")
    parts.append("```")

    # STDOUT — context truoc khi crash
    parts.append("")
    parts.append("## STDOUT LOG (.out) — 80 dong cuoi")
    parts.append("```")
    parts.append(stdout_content[:5000] if stdout_content.strip() else "(rong)")
    parts.append("```")

    # Supervisor config
    parts.append("")
    parts.append(f"## SUPERVISOR CONFIG — [program:{process_name}]")
    parts.append("```ini")
    parts.append(supervisor_conf if supervisor_conf.strip() else "(khong tim thay)")
    parts.append("```")

    # Trang thai he thong
    parts.append("")
    parts.append("## TRANG THAI HE THONG")
    parts.append(f"MEM_FREE_MB:      {mem_free_mb}")
    parts.append(f"DISK_USAGE_PCT:   {disk_pct}")
    parts.append(f"OOM_IN_SYSLOG:    {oom_flag}")
    parts.append(f"SIGNAL_IN_SYSLOG: {signal_flag}")

    if mem_detail and mem_detail.strip():
        parts.append("")
        parts.append("## MEMORY DETAIL (free -m)")
        parts.append("```")
        parts.append(mem_detail.strip()[:1000])
        parts.append("```")

    if disk_detail and disk_detail.strip():
        parts.append("")
        parts.append("## DISK DETAIL (df -h)")
        parts.append("```")
        parts.append(disk_detail.strip()[:1500])
        parts.append("```")

    # Process detail — PID, RSS, threads, FD
    if proc_detail and proc_detail.strip() and "Process not running" not in proc_detail:
        parts.append("")
        parts.append("## PROCESS RUNTIME INFO")
        parts.append("```")
        parts.append(proc_detail.strip()[:2000])
        parts.append("```")

    # Process environment (filtered — no secrets)
    if proc_env and proc_env.strip() and "Process not running" not in proc_env:
        parts.append("")
        parts.append("## PROCESS ENVIRONMENT (filtered)")
        parts.append("```")
        parts.append(proc_env.strip()[:2000])
        parts.append("```")

    # Lich su restart
    if restart_history and restart_history.strip():
        parts.append("")
        parts.append("## LICH SU RESTART (40 dong cuoi)")
        parts.append("```")
        parts.append(restart_history.strip()[:3000])
        parts.append("```")
    elif supervisord_log and supervisord_log.strip():
        parts.append("")
        parts.append("## SUPERVISORD LOG — 80 dong cuoi")
        parts.append("```")
        parts.append(supervisord_log[:4000])
        parts.append("```")

    if dmesg_recent and dmesg_recent.strip():
        parts.append("")
        parts.append("## DMESG RECENT — 40 dong cuoi")
        parts.append("```")
        parts.append(dmesg_recent[:3000])
        parts.append("```")

    if top_mem and top_mem.strip():
        parts.append("")
        parts.append("## TOP PROCESSES BY MEMORY (RSS)")
        parts.append("```")
        parts.append(top_mem[:2000])
        parts.append("```")

    if top_cpu and top_cpu.strip():
        parts.append("")
        parts.append("## TOP PROCESSES BY CPU")
        parts.append("```")
        parts.append(top_cpu[:1500])
        parts.append("```")

    if network_info and network_info.strip():
        parts.append("")
        parts.append("## LISTENING PORTS (ss -tlnp)")
        parts.append("```")
        parts.append(network_info.strip()[:1500])
        parts.append("```")

    if journal_log and journal_log.strip():
        parts.append("")
        parts.append("## SYSTEMD JOURNAL")
        parts.append("```")
        parts.append(journal_log.strip()[:2000])
        parts.append("```")

    # === THONG TIN FILE PATHS (rat quan trong cho CONFIG_ERR / PERM_ERR) ===
    if referenced_paths and referenced_paths.strip():
        parts.append("")
        parts.append("## FILE/DIRECTORY TRONG STDERR (cac path bi loi)")
        parts.append("Listing cac thu muc chua file duoc mention trong error log.")
        parts.append("NEU CO FILE .bak / .orig / .old → DUNG cp DE KHOI PHUC.")
        parts.append("```")
        parts.append(referenced_paths.strip()[:3000])
        parts.append("```")

    if workdir_files and workdir_files.strip():
        parts.append("")
        parts.append("## WORKING DIRECTORY CUA PROCESS")
        parts.append("Cac file trong thu muc lam viec + backup files neu co.")
        parts.append("```")
        parts.append(workdir_files.strip()[:2500])
        parts.append("```")

    if source_snippets and source_snippets.strip():
        parts.append("")
        parts.append("## SOURCE CODE CUA APP (file .py/.sh mention trong traceback)")
        parts.append("DUNG DE SUY LUAN: config cần format gì, field gì, gọi dep gì.")
        parts.append("```")
        parts.append(source_snippets.strip()[:4000])
        parts.append("```")

    if similar_configs and similar_configs.strip():
        parts.append("")
        parts.append("## SIMILAR CONFIG FILES (cùng extension trong thư mục lân cận)")
        parts.append("DUNG DE SUY LUAN: file config bi thieu CO THE co format giong cac file nay.")
        parts.append("```")
        parts.append(similar_configs.strip()[:3500])
        parts.append("```")

    if git_context and git_context.strip():
        parts.append("")
        parts.append("## GIT CONTEXT (nếu có)")
        parts.append("DUNG DE: rollback deploy gần đây, biết file vừa bị thay đổi.")
        parts.append("```")
        parts.append(git_context.strip()[:2000])
        parts.append("```")

    parts.append("")
    parts.append("## ==== HET DU LIEU ==== ##")

    return "\n".join(parts)


def build_supervisor_llm_prompt(evidence_pack: str) -> str:
    """Build the complete one-shot prompt for Supervisor LLM analysis."""
    return f"""{SUPERVISOR_SYSTEM_PROMPT}

=== DỮ LIỆU THU THẬP ===

{evidence_pack}

=== KẾT THÚC DỮ LIỆU ===

Hãy phân tích toàn bộ dữ liệu trên theo 5 bước và trả về JSON theo schema sau.
Nhớ:
- category phải là 1 trong: OOM, CRASH_LOOP, DEP_FAIL, CONFIG_ERR, PERM_ERR, CODE_ERR, RESOURCE, SIGNAL, DEP_VERSION, UNKNOWN
- severity phải là: CRITICAL, HIGH, MEDIUM, hoặc LOW
- immediate_action.commands phải là danh sách lệnh bash thực thi được ngay
- summary_vi bằng tiếng Việt, dưới 20 từ

JSON Schema:
{SUPERVISOR_RESPONSE_SCHEMA}

Trả về JSON:"""
