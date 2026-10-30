from services.workflow_notifications import notify_staff
from typing import Annotated, Literal
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from core.database import get_supabase
from core.dependencies import require_staff, require_roles
from models.report import ReportGenerate, ReportReviewComplete
from services.report_service import report_options, fetch_snapshot, snapshot_hash, render_html, render_csv, read_all

router = APIRouter(prefix="/api/reports", tags=["Reports"])
logger = logging.getLogger(__name__)


def build_report(body, user):
    try:
        options = report_options(body, user)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        return fetch_snapshot(options, user)
    except Exception as exc:
        logger.exception("Report compilation failed")
        raise HTTPException(503, "Could not compile report data. Check database connectivity and the status history / forecast migrations; no report was archived.") from exc


def saved_report(report_id, user):
    rows = get_supabase().table("reports").select("*").eq("id", report_id).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, "Report not found")
    report = rows[0]
    if user.get("role") == "city_health_officer" and not report.get("submitted_at"):
        raise HTTPException(403, "This report has not been sent to CHU.")
    if user.get("role") == "barangay_official":
        if not user.get("barangay") or (report.get("barangay") or "").strip().lower() != user["barangay"].strip().lower():
            raise HTTPException(403, "You can only access reports for your assigned barangay.")
    if not report.get("snapshot"):
        raise HTTPException(409, "This legacy report has no archived data. Generate a new report for the desired period.")
    if report.get("schema_version") != 1:
        raise HTTPException(409, "This report format is not supported by this server.")
    if snapshot_hash(report["snapshot"]) != report.get("content_sha256"):
        raise HTTPException(409, "The report data does not match its archived fingerprint.")
    return report


@router.get("", include_in_schema=False)
@router.get("/")
async def list_reports(current_user: Annotated[dict, Depends(require_staff)]):
    query = get_supabase().table("reports").select(
        "id,title,type,barangay,period_start,period_end,generated_by,author_name,status,created_at,schema_version,content_sha256,"
        "review_status,submitted_at,submitted_by,submitted_by_name,review_started_at,review_started_by,reviewer_name,reviewed_at,reviewed_by,reviewed_by_name"
    ).order("created_at", desc=True).order("id")
    if current_user.get("role") == "barangay_official":
        if not current_user.get("barangay"):
            raise HTTPException(403, "Your account has no assigned barangay.")
        query = query.ilike("barangay", current_user["barangay"].strip())
    elif current_user.get("role") == "city_health_officer":
        query = query.in_("review_status", ["submitted", "in_review", "done"])
    try:
        return {"success": True, "data": read_all(query)}
    except Exception as exc:
        logger.exception("Report archive unavailable")
        raise HTTPException(503, "Report archive is unavailable. Check the report snapshots and review workflow migrations.") from exc


@router.get("/pending-count")
async def pending_report_count(current_user: Annotated[dict, Depends(require_staff)]):
    """Small, role-scoped count of the queue awaiting its next review action."""
    role = current_user.get('role')
    if role not in ('barangay_official', 'sanitization_inspector', 'city_health_officer'):
        raise HTTPException(403, 'This account has no staff report queue.')
    if role == 'barangay_official' and not (current_user.get('barangay') or '').strip():
        raise HTTPException(403, 'Your account has no assigned barangay.')
    try:
        sb = get_supabase()
        if role == 'barangay_official':
            query = (sb.table('resident_reports').select('id', count='exact', head=True)
                     .eq('barangay', current_user['barangay'])
                     .or_('and(or(status.is.null,status.eq.pending,status.eq.""),or(type.is.null,type.neq.unregistered_source))'))
            route = '/validate-reports'
        elif role == 'sanitization_inspector':
            # Shared city-wide queue, exactly like the Pending Review tab.
            # Count tickets only: a linked resident report is not a second item.
            query = (sb.table('inspection_requests').select('id', count='exact', head=True)
                     .or_('status.is.null,status.eq.pending,status.eq.assigned,status.eq.""'))
            route = '/inspections'
        else:
            query = (sb.table('reports').select('id', count='exact', head=True)
                     .eq('review_status', 'submitted').not_.is_('submitted_at', 'null'))
            route = '/view-reports'
        count = query.execute().count
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError('Report count unavailable')
        return {'success': True, 'count': count, 'route': route}
    except Exception as exc:
        logger.exception('Pending report count unavailable')
        raise HTTPException(503, 'Pending report count is unavailable.') from exc


@router.post("/preview", response_class=HTMLResponse)
async def preview_report(body: ReportGenerate, current_user: Annotated[dict, Depends(require_roles("admin", "sanitization_inspector"))]):
    snapshot = build_report(body, current_user)
    return HTMLResponse(render_html(snapshot), headers={"Cache-Control": "no-store"})


@router.post("/generate")
async def generate_report(body: ReportGenerate, current_user: Annotated[dict, Depends(require_roles("admin", "sanitization_inspector"))]):
    snapshot = build_report(body, current_user)
    row = {
        "title": snapshot["title"], "type": "water_surveillance", "barangay": snapshot["barangay"],
        "period_start": snapshot["period_start"], "period_end": snapshot["period_end"],
        "generated_by": current_user["id"], "status": "completed", "file_url": None,
        "author_name": snapshot["prepared_by"],
        "snapshot": snapshot, "schema_version": 1, "content_sha256": snapshot_hash(snapshot),
    }
    try:
        result = get_supabase().table("reports").insert(row).execute()
    except Exception as exc:
        logger.exception("Could not archive compiled report")
        raise HTTPException(503, "Report compiled but could not be archived. Check the report snapshots and review workflow migrations and retry.") from exc
    if not result.data:
        raise HTTPException(500, "The report was not archived.")
    report = result.data[0]
    report.pop("snapshot", None)
    return {"success": True, "message": "Report generated and archived. Use Send to CHU Doctor to submit it for review.", "data": report}


def report_transition(report_id, user, action, notes=None):
    report = saved_report(report_id, user)
    if action == "send":
        if user.get("role") != "admin" and report.get("generated_by") != user["id"]:
            raise HTTPException(403, "Only the report author can send this report.")
        function = "send_report_to_chu"
        parameters = {"p_report_id": report_id, "p_actor_id": user["id"]}
    else:
        if not report.get("submitted_at"):
            raise HTTPException(409, "The report has not been sent to CHU.")
        if report.get("review_status") != "done" and report.get("review_started_by") not in (None, user["id"]) and user.get("role") != "admin":
            raise HTTPException(409, "This report is being reviewed by another doctor.")
        if action == "complete" and not (notes or "").strip():
            raise HTTPException(422, "Record review notes before marking the report Done.")
        function = "review_archived_report"
        parameters = {"p_report_id": report_id, "p_actor_id": user["id"], "p_action": action, "p_notes": notes}
    try:
        result = get_supabase().rpc(function, parameters).execute().data
    except Exception as exc:
        logger.exception("Report workflow transition failed")
        known = ["Only the report author can send this report", "This report is being reviewed by another doctor",
                 "Start review before marking the report Done", "The report has not been sent to CHU",
                 "Record review notes before marking the report Done"]
        for message in known:
            if message in str(exc):
                raise HTTPException(409, message) from exc
        raise HTTPException(503, "Could not update the report workflow. Refresh and retry; check the report review migration if the issue persists.") from exc
    if isinstance(result, list):
        result = result[0] if result else None
    if not result:
        raise HTTPException(500, "The report update returned no receipt.")
    result.pop("snapshot", None)
    return {"success": True, "data": result}


@router.post("/{report_id}/send")
async def send_report(report_id: str, current_user: Annotated[dict, Depends(require_roles("sanitization_inspector", "admin"))]):
    before = saved_report(report_id, current_user)
    result = report_transition(report_id, current_user, "send")
    if not before.get("submitted_at"):
        await notify_staff(["city_health_officer"], "New report received",
                           "A report has been sent to you for review.", "/view-reports",
                           f"doctor-report-{report_id}")
    return result


@router.post("/{report_id}/review/start")
async def start_report_review(report_id: str, current_user: Annotated[dict, Depends(require_roles("city_health_officer", "admin"))]):
    return report_transition(report_id, current_user, "start")


@router.post("/{report_id}/review/complete")
async def complete_report_review(report_id: str, body: ReportReviewComplete,
                                 current_user: Annotated[dict, Depends(require_roles("city_health_officer", "admin"))]):
    return report_transition(report_id, current_user, "complete", body.notes.strip())


@router.get("/{report_id}")
async def report_detail(report_id: str, current_user: Annotated[dict, Depends(require_staff)]):
    report = saved_report(report_id, current_user)
    try:
        events = get_supabase().table("report_review_events").select("*").eq("report_id", report_id).order("created_at").execute().data or []
    except Exception as exc:
        raise HTTPException(503, "Could not load the report review trail. Check the report review migration.") from exc
    return {"success": True, "data": report, "events": events}


@router.get("/{report_id}/download")
async def download_report(
    report_id: str, current_user: Annotated[dict, Depends(require_staff)],
    format: Literal["csv", "html"] = Query("csv"),
):
    report = saved_report(report_id, current_user)
    if format == "html":
        content = render_html(report["snapshot"], report["id"], report["content_sha256"], review=report)
        media_type = "text/html"
    else:
        content = render_csv(report["snapshot"], report["id"], report["content_sha256"], review=report)
        media_type = "text/csv"
    # Use only the database UUID in the filename, never a user-entered title.
    filename = "WaterWatch-" + str(report["id"]) + "." + format
    return Response(content=content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })
