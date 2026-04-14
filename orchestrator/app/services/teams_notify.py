"""MS Teams notification via Power Automate webhook."""
import httpx
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


async def send_teams_notification(
    incident_id, incident_number, alert_name, instance, severity,
    root_cause_summary, confidence, options, approve_base_url,
):
    s = get_settings()
    if not s.teams_enabled or not s.teams_webhook_url:
        logger.info("[TEAMS] Disabled or no webhook")
        return False

    approve_url = f"{approve_base_url}/incidents/{incident_id}"

    # Build options text
    opts_lines = []
    for i, o in enumerate(options[:5], 1):
        title = o.get("title", "")
        risk = o.get("risk_level", "")
        cmds = o.get("commands_json") or o.get("commands") or []
        cmd_preview = cmds[0][:80] if cmds else ""
        opts_lines.append(f"#{i} {title} [{risk}]" + (f"\n   `{cmd_preview}`" if cmd_preview else ""))

    opts_text = "\n".join(opts_lines) if opts_lines else "Chưa có phương án"
    conf_pct = int((confidence or 0) * 100)

    # Power Automate format — simple JSON payload
    # Power Automate workflow will receive these fields and format the Teams message
    payload = {
        "incident_id": incident_id,
        "incident_number": incident_number,
        "alert_name": alert_name,
        "instance": instance,
        "severity": severity,
        "confidence": conf_pct,
        "root_cause": root_cause_summary or "Đang phân tích...",
        "remediation_options": opts_text,
        "approve_url": approve_url,
        # Also include a pre-formatted text for simple webhook setups
        "text": (
            f"🚨 **{alert_name}** trên `{instance}`\n\n"
            f"**Severity:** {severity} | **Confidence:** {conf_pct}%\n"
            f"**Incident:** {incident_number}\n\n"
            f"---\n\n"
            f"**🔍 Root Cause:**\n{root_cause_summary or 'Đang phân tích...'}\n\n"
            f"---\n\n"
            f"**⚡ Phương án xử lý:**\n{opts_text}\n\n"
            f"---\n\n"
            f"[👉 Xem chi tiết & Approve]({approve_url})"
        ),
    }

    logger.info(f"[TEAMS] Sending notification for {incident_number}")
    logger.info(f"[TEAMS] URL: {approve_url}")
    logger.info(f"[TEAMS] Root cause: {root_cause_summary[:100] if root_cause_summary else 'N/A'}")

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(s.teams_webhook_url, json=payload)
            logger.info(f"[TEAMS] Response: {r.status_code} {r.text[:200]}")
            ok = r.status_code in (200, 201, 202)
            if ok:
                logger.info(f"[TEAMS] ✅ Sent for {incident_number}")
            else:
                logger.error(f"[TEAMS] ❌ HTTP {r.status_code}: {r.text[:500]}")
            return ok
    except Exception as e:
        logger.error(f"[TEAMS] ❌ Error: {e}")
        return False
