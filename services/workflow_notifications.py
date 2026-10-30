"""Targeted workflow push delivery. Delivery failures never undo saved reports."""
import logging
from core.database import get_supabase
from services.push_service import broadcast_push_notification

logger = logging.getLogger(__name__)


async def notify_linked_resident_outcome(report_id):
    try:
        rows = get_supabase().table("resident_reports").select("*").eq("id", report_id).execute().data or []
        if rows:
            await notify_resident_outcome(rows[0])
    except Exception:
        logger.exception("Could not load report outcome for push: %s", report_id)


async def notify_staff(roles, title, message, url, event_key, barangay=None):
    try:
        offset = 0
        while True:
            query = (get_supabase().table("users").select("id,role,barangay")
                     .in_("role", roles).eq("is_active", True).order("id"))
            rows = query.range(offset, offset + 499).execute().data or []
            for user in rows:
                if user["role"] == "barangay_official" and (
                    not barangay or (user.get("barangay") or "").strip().casefold() != barangay.strip().casefold()
                ):
                    continue
                await broadcast_push_notification(title, message, user_id=user["id"], url=url, tag=event_key)
            if len(rows) < 500:
                break
            offset += 500
    except Exception:
        logger.exception("Workflow push delivery failed for %s", event_key)


async def notify_resident_outcome(report):
    status = report.get("status")
    if status not in ("done", "completed", "resolved", "dismissed", "rejected") or not report.get("submitted_by"):
        return
    outcome = "dismissed" if status in ("dismissed", "rejected") else "done"
    kind = "Water source submission" if report.get("type") == "unregistered_source" else "Concern"
    try:
        await broadcast_push_notification(
            f"{kind} marked {outcome}",
            report.get("reason") or f"Your {kind.lower()} has been reviewed and marked {outcome}.",
            user_id=report["submitted_by"], url="/portal/notifications",
            tag=f"report-outcome-{report['id']}-{outcome}",
        )
    except Exception:
        logger.exception("Resident outcome push failed for %s", report.get("id"))
