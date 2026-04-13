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

=== MÔI TRƯỜNG THỰC THI (RẤT QUAN TRỌNG) ===

Commands sẽ được chạy qua SSH với user `devops` (non-root).
User này có NOPASSWD sudo CHỈ cho danh sách lệnh sau:
  supervisorctl, supervisord, systemctl, cp, mv, rm, mkdir,
  chmod, chown, chgrp, tee, kill, pkill, dmesg, journalctl, ss, lsof,
  ls, find, cat, head, tail

QUY TẮC BẮT BUỘC:
  - MỌI lệnh ghi/sửa file hệ thống PHẢI bắt đầu bằng `sudo`
  - VD ĐÚNG:   sudo cp /opt/test-apps/api_config.json.bak /opt/test-apps/api_config.json
  - VD SAI:    cp /opt/test-apps/api_config.json.bak /opt/test-apps/api_config.json
               (sẽ fail Permission denied)
  - Với `tee` để ghi file cần sudo: echo '...' | sudo tee /path/file
  - KHÔNG dùng shell redirect `>` vì sudo không pass qua được redirect
    VD SAI: sudo echo 'xxx' > /path  (redirect chạy với quyền user, không phải sudo)
    VD ĐÚNG: echo 'xxx' | sudo tee /path

=== NGUYÊN TẮC ĐƯA COMMANDS (QUAN TRỌNG) ===

immediate_action.commands phải THỰC SỰ FIX được vấn đề, KHÔNG PHẢI chỉ kiểm tra.

[CONFIG_ERR] — file config thiếu/sai — ƯU TIÊN THEO DECISION TREE:

  ƯU TIÊN 1: Có file backup?
    → Check section "WORKING DIRECTORY" và "FILE/DIRECTORY TRONG STDERR"
    → Tìm file cùng tên + đuôi .bak, .orig, .old, ~
    → NẾU CÓ: sudo cp <backup> <missing_file> && sudo supervisorctl restart <process>

  ƯU TIÊN 2: Có git repo?
    → Check section "GIT CONTEXT"
    → NẾU có git VÀ file đã từng commit:
      cd <workdir> && git checkout HEAD -- <missing_file>
      sudo supervisorctl restart <process>

  ƯU TIÊN 3: Có file cùng format lân cận?
    → Check section "SIMILAR CONFIG FILES"
    → NẾU có file .json/.yaml/.conf khác tương tự:
      sudo cp <similar_file> <missing_file>  # rồi dặn operator edit cho đúng
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


def _is_useless(text: str) -> bool:
    """Return True nếu section khong chua thong tin co ich → skip."""
    if not text or not text.strip():
        return True
    t = text.strip().lower()
    useless_markers = (
        "not found", "not running", "khong tim thay", "(rong)", "(empty)",
        "no working directory", "not a git repo", "no config",
        "cannot read", "permission denied",
    )
    # Neu content < 20 ky tu VA chi chua marker vo ich
    if len(t) < 80 and any(m in t for m in useless_markers):
        return True
    return False


def _compress_stderr(text: str, max_chars: int = 4000) -> str:
    """Nén stderr: ưu tiên giữ traceback + exception message, bỏ debug noise.

    Stderr thường có pattern: log debug/info dài → cuối cùng là Traceback + Exception.
    Giữ traceback (quan trọng nhất) + vài dòng context trước đó.
    """
    if not text or not text.strip():
        return "(rong)"
    if len(text) <= max_chars:
        return text

    lines = text.splitlines()
    # Tìm line chứa Traceback hoặc Exception name
    tb_idx = -1
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" in line:
            tb_idx = i
            break
    if tb_idx == -1:
        # Không có traceback → giữ 100 dòng cuối
        return "\n".join(lines[-100:])[:max_chars]

    # Giữ 10 dòng context trước traceback + toàn bộ traceback đến hết
    start = max(0, tb_idx - 10)
    compressed = "\n".join(lines[start:])
    if len(compressed) > max_chars:
        # Nếu traceback quá dài, giữ đầu (exception type) + cuối (exception message)
        compressed = compressed[:max_chars // 2] + "\n...[TRUNCATED]...\n" + compressed[-max_chars // 2:]
    return compressed


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

    def add(title: str, content: str, max_chars: int = 1500, lang: str = "", hint: str = ""):
        """Helper: only add section if content is useful."""
        if _is_useless(content):
            return
        parts.append("")
        parts.append(f"## {title}")
        if hint:
            parts.append(hint)
        parts.append(f"```{lang}")
        parts.append(content.strip()[:max_chars])
        parts.append("```")

    # STDERR — quan trong nhat, dung compress de giu traceback
    parts.append("")
    parts.append("## STDERR (traceback + exception)")
    parts.append("```")
    parts.append(_compress_stderr(stderr_content, max_chars=4000))
    parts.append("```")

    add("STDOUT LOG (tail)", stdout_content, 2000)
    add(f"SUPERVISOR CONFIG [program:{process_name}]", supervisor_conf, 1500, lang="ini")

    # Trang thai he thong — 1 dong gon
    parts.append("")
    parts.append("## SYSTEM STATE")
    parts.append(f"MEM_FREE_MB={mem_free_mb} | DISK_PCT={disk_pct} | OOM={oom_flag} | SIG_FLAG={signal_flag}")

    add("MEMORY DETAIL (free -m)", mem_detail, 600)
    add("DISK DETAIL (df -h)", disk_detail, 800)
    add("PROCESS RUNTIME INFO", proc_detail, 1200)
    add("PROCESS ENV (filtered)", proc_env, 1200)

    # Restart history ưu tiên, fallback sang supervisord log
    if not _is_useless(restart_history):
        add("RESTART HISTORY", restart_history, 1500)
    else:
        add("SUPERVISORD LOG (tail)", supervisord_log, 2000)

    add("DMESG RECENT", dmesg_recent, 1500)
    add("TOP BY MEM (RSS)", top_mem, 1200)
    add("TOP BY CPU", top_cpu, 800)
    add("LISTENING PORTS", network_info, 800)
    add("SYSTEMD JOURNAL", journal_log, 1200)

    # === REMEDIATION-ORIENTED EVIDENCE (quan trong nhat sau stderr) ===
    add("FILE PATHS TRONG STDERR (+ directory listing)",
        referenced_paths, 2000,
        hint="Neu thay file .bak/.orig/.old → dung cp de khoi phuc.")

    add("WORKING DIRECTORY (backup files neu co)", workdir_files, 1500)

    add("SOURCE CODE CUA APP (file trong traceback)",
        source_snippets, 2500,
        hint="Dung de suy luan: config can fields gi, app goi dep gi.")

    add("SIMILAR CONFIG FILES (cung extension trong thu muc lan can)",
        similar_configs, 2500,
        hint="File config bi thieu CO THE co format giong cac file nay.")

    add("GIT CONTEXT (neu co)", git_context, 1500,
        hint="Dung de rollback deploy gan day, biet file vua bi thay doi.")

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
