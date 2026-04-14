"""One-shot LLM prompt template for Host Resource RCA."""

SYSTEM_PROMPT = """Bạn là một Senior SRE / AIOps Engineer chuyên phân tích root cause alert CPU / RAM / DISK trên Linux host.

Bạn sẽ nhận được một evidence pack gồm nhiều block dữ liệu thu thập từ Prometheus metrics và SSH commands.
Nhiệm vụ của bạn là:
1. Phân tích toàn bộ evidence
2. Xác định root cause SÂU NHẤT có thể (không chỉ symptom bề mặt)
3. Phân biệt rõ symptom, immediate cause, contributing factors, và root cause thật
4. Đánh giá mức độ tin cậy
5. Đề xuất TỐI THIỂU 3 phương án xử lý với risk level, commands, rollback, pre/post checks

QUAN TRỌNG:
- Không được chỉ nói "CPU cao do process X" mà phải giải thích TẠI SAO process X lại cao
- Nếu thấy GC log → kết luận heap pressure
- Nếu thấy iowait cao → gốc ở disk, không phải CPU
- Nếu thấy steal time → gốc ở hypervisor
- Nếu thấy cron/backup gần thời điểm alert → correlation
- Nếu thấy deleted-open-file → disk đầy nhưng du không thấy
- Nếu thấy zombie/D-state → giải thích tác động
- remediation_options PHẢI có TỐI THIỂU 3 phương án
- Mỗi phương án PHẢI có commands, rollback_commands, pre_checks, post_checks
- operator_message_vi PHẢI viết bằng tiếng Việt ngắn gọn dễ hiểu

Trả lời ĐÚNG JSON format bên dưới, KHÔNG có text ngoài JSON."""


RESPONSE_SCHEMA = """{
  "symptom": "mô tả triệu chứng bằng tiếng Việt",
  "immediate_cause": "nguyên nhân trực tiếp bằng tiếng Việt",
  "contributing_factors": ["yếu tố góp phần 1", "yếu tố 2"],
  "root_cause_hypothesis": "giả thuyết root cause sâu nhất bằng tiếng Việt",
  "why_not_just_symptom": "giải thích tại sao đây không chỉ là symptom",
  "rca_level": "symptom_only|probable_root_cause|verified_root_cause",
  "verification_status": "weak|medium|strong",
  "confidence": 0.85,
  "impact": "low|medium|high|critical",
  "suspected_service": "tên service nghi vấn",
  "suspected_job": "tên job/cron nghi vấn",
  "suspected_path": "đường dẫn file/dir nghi vấn",
  "suspected_mount": "mount point nghi vấn",
  "canonical_root_cause": "tên_root_cause_chuẩn_hóa_snake_case",
  "issue_subtype": "phân loại con",
  "evidence_refs": ["command_id hoặc metric_name đã dùng làm bằng chứng"],
  "what_is_still_unknown": ["những gì chưa kết luận được — tiếng Việt"],
  "summary": "tóm tắt RCA đầy đủ bằng tiếng Việt, chi tiết nguyên nhân và tác động",
  "root_causes": [
    {"name": "tên root cause tiếng Việt", "confidence": 0.85, "why": "giải thích bằng tiếng Việt", "evidence_refs": ["ref1"]}
  ],
  "remediation_options": [
    {
      "option_id": "opt-1",
      "priority": 1,
      "title": "tiêu đề phương án bằng tiếng Việt",
      "description": "mô tả chi tiết bằng tiếng Việt",
      "risk_level": "low|medium|high|critical",
      "needs_approval": true,
      "action_type": "restart|kill|cleanup|config|manual",
      "target": "service hoặc path",
      "params": {},
      "commands": ["lệnh Linux thực tế 1", "lệnh 2", "lệnh 3"],
      "expected_effect": "kỳ vọng sau khi thực hiện — tiếng Việt",
      "rollback_commands": ["lệnh rollback nếu cần"],
      "pre_checks": ["kiểm tra trước khi thực hiện"],
      "post_checks": ["kiểm tra sau khi thực hiện"],
      "warnings": ["cảnh báo rủi ro — tiếng Việt"]
    }
  ],
  "recommended_option": "opt-1",
  "operator_message_vi": "Tin nhắn tiếng Việt ngắn gọn cho DevOps: root cause là gì, nên làm gì ngay",
  "warnings": ["cảnh báo chung — tiếng Việt"]
}"""


def build_llm_prompt(evidence_pack: str) -> str:
    """Build the complete one-shot prompt for LLM."""
    return f"""{SYSTEM_PROMPT}

=== EVIDENCE PACK ===

{evidence_pack}

=== END EVIDENCE PACK ===

Hãy phân tích evidence pack trên và trả về JSON theo schema sau.

QUY TẮC BẮT BUỘC:
- TẤT CẢ nội dung text (title, description, summary, expected_effect, warnings, operator_message_vi) PHẢI viết bằng TIẾNG VIỆT
- TỐI THIỂU 3 remediation_options, ưu tiên 3-5 phương án
- Mỗi phương án phải đủ commands (lệnh thực tế chạy được), rollback_commands, warnings
- commands phải là lệnh Linux thực tế, cụ thể với PID/service/path lấy từ evidence
- operator_message_vi: tóm tắt ngắn gọn tiếng Việt cho DevOps hiểu ngay root cause và nên làm gì
- Viết đầy đủ chi tiết, KHÔNG viết tắt, KHÔNG cắt ngắn

JSON Schema:
{RESPONSE_SCHEMA}

Trả về JSON:"""
