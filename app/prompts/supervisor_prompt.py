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
- Luôn trả JSON hợp lệ dù log rỗng"""


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

    parts.append("")
    parts.append("## STDERR LOG (.err) — 80 dòng cuối")
    parts.append("```")
    parts.append(stderr_content if stderr_content.strip() else "(rỗng)")
    parts.append("```")

    parts.append("")
    parts.append("## STDOUT LOG (.out) — 40 dòng cuối")
    parts.append("```")
    parts.append(stdout_content if stdout_content.strip() else "(rỗng)")
    parts.append("```")

    parts.append("")
    parts.append(f"## SUPERVISOR CONFIG — [program:{process_name}]")
    parts.append("```ini")
    parts.append(supervisor_conf if supervisor_conf.strip() else "(không tìm thấy)")
    parts.append("```")

    parts.append("")
    parts.append("## TRẠNG THÁI HỆ THỐNG")
    parts.append(f"MEM_FREE_MB:      {mem_free_mb}")
    parts.append(f"DISK_USAGE_PCT:   {disk_pct}")
    parts.append(f"OOM_IN_SYSLOG:    {oom_flag}")
    parts.append(f"SIGNAL_IN_SYSLOG: {signal_flag}")

    if supervisord_log.strip():
        parts.append("")
        parts.append("## SUPERVISORD LOG — 50 dòng cuối")
        parts.append("```")
        parts.append(supervisord_log[:3000])
        parts.append("```")

    if dmesg_recent.strip():
        parts.append("")
        parts.append("## DMESG RECENT — 30 dòng cuối")
        parts.append("```")
        parts.append(dmesg_recent[:2000])
        parts.append("```")

    if top_mem.strip():
        parts.append("")
        parts.append("## TOP PROCESSES BY MEMORY")
        parts.append("```")
        parts.append(top_mem[:2000])
        parts.append("```")

    parts.append("")
    parts.append("## ==== HẾT DỮ LIỆU ==== ##")

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
