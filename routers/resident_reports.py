from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, require_roles
from core.storage import upload_to_supabase, delete_from_supabase
from models.resident_report import ResidentReportCreate, ResidentReportAction

router = APIRouter(prefix="/api/resident-reports", tags=["Resident Reports"])

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


@router.post("/upload-image")
async def upload_resident_report_image(
    file: UploadFile = File(...),
    current_user: Annotated[dict, Depends(require_roles("resident", "barangay_official", "admin", "sanitization_inspector", "city_health_officer"))] = None,
):
    """Upload an optional photo proof for a resident report to Supabase Storage."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}")

    contents = await file.read()
    if len(contents) > 16 * 1024 * 1024:  # 16MB limit
        raise HTTPException(status_code=400, detail="File too large. Max 16MB.")

    public_url = await upload_to_supabase(contents, file.filename, file.content_type or "image/jpeg")

    return {"success": True, "message": "Image uploaded", "image_url": public_url}


@router.get("", include_in_schema=False)
@router.get("/")
async def list_resident_reports(
    current_user: Annotated[dict, Depends(require_roles("admin", "barangay_official", "resident", "sanitization_inspector", "city_health_officer"))]
):
    """List resident-submitted reports. Barangay officials see their own barangay only."""
    import re
    sb = get_supabase()
    query = sb.table("resident_reports").select("*").order("created_at", desc=True)

    if current_user["role"] == "barangay_official":
        if not (current_user.get("barangay") or "").strip():
            raise HTTPException(403, "Your account has no assigned barangay. Contact an administrator.")
        query = query.eq("barangay", current_user["barangay"])
    elif current_user["role"] == "resident":
        query = query.eq("submitted_by", current_user["id"])
    elif current_user["role"] in ("sanitization_inspector", "city_health_officer"):
        # Sanitization inspector & CHU ONLY see reports that have been endorsed/escalated by barangay officials
        query = query.in_("status", ["escalated", "passed_to_chu", "assigned", "in_progress", "processing", "completed", "resolved", "done", "dismissed"])

    result = query.execute()
    data = result.data or []

    # Ensure image_url is populated even if stored in description fallback
    for item in data:
        desc = item.get("description") or ""
        if not item.get("image_url") and not item.get("photo_url"):
            match = re.search(r'\[(?:Attached )?Photo Proof:\s*([^\]]+)\]', desc, re.IGNORECASE)
            if match:
                item["image_url"] = match.group(1).strip()
                item["photo_url"] = match.group(1).strip()
        
        # Clean description for display
        item["clean_description"] = re.sub(r'\[(?:Attached )?Photo Proof:[^\]]*\]', '', desc, flags=re.IGNORECASE).strip()

        # Map rejected status to done or dismissed for Done History
        st = item.get("status")
        rs = item.get("reason") or ""
        if st in ("rejected", "done", "dismissed"):
            if st == "done" or rs.startswith("[DONE]"):
                item["status"] = "done"
                item["reason"] = rs.replace("[DONE]", "").strip()
            else:
                item["status"] = "dismissed"
                item["reason"] = rs.replace("[DISMISSED]", "").strip()

    return {"success": True, "data": data}


@router.post("", include_in_schema=False)
@router.post("/")
async def submit_resident_report(
    body: ResidentReportCreate,
    current_user: Annotated[dict, Depends(require_roles("resident", "barangay_official"))]
):
    """Residents submit reports; officials submit validated concerns for CHU forwarding."""
    sb = get_supabase()
    
    report_type = body.category or body.type or "concern"
    is_official = current_user.get("role") == "barangay_official"
    if is_official and report_type == "unregistered_source":
        raise HTTPException(403, "Use the Water Sources page to register a new source.")
    location = None
    if report_type != "unregistered_source":
        if not body.location_id:
            raise HTTPException(422, "Select a water source before sending your request.")
        locations = (sb.table("water_locations").select("id, full_name, barangay, latitude, longitude")
                     .eq("id", str(body.location_id)).is_("archived_at", "null").execute().data or [])
        if not locations:
            raise HTTPException(404, "The selected water source no longer exists. Select another source.")
        location = locations[0]
        if not current_user.get("barangay") or (location.get("barangay") or "").strip().lower() != current_user["barangay"].strip().lower():
            raise HTTPException(403, "Select a water source in your assigned barangay.")
    type_label = report_type.replace("_", " ").title()
    report_title = (body.title or "").strip() or f"{type_label} Report"
    img_url = body.image_url or body.photo_url
    
    desc = body.description.strip()
    if location:
        import re
        desc = re.sub(r"\[Inspected Source:[^\]]*\]", "", desc, flags=re.IGNORECASE).strip()
        desc += f"\n\n[Inspected Source: {location['full_name']} (ID: {location['id']})]"
    if img_url and "[Attached Photo Proof:" not in desc:
        desc = f"{desc}\n\n[Attached Photo Proof: {img_url}]"

    new_report = {
        "title": report_title,
        "description": desc,
        "type": report_type,
        "latitude": location["latitude"] if location else body.latitude,
        "longitude": location["longitude"] if location else body.longitude,
        "barangay": location["barangay"] if location else (body.barangay or current_user.get("barangay")),
        "submitted_by": current_user["id"],
        "status": "validated" if is_official else "pending"
    }

    try:
        # Try inserting without image_url column first as it is in the description
        result = sb.table("resident_reports").insert(new_report).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save report: {str(e)}")

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to submit report")

    row = result.data[0]
    row["image_url"] = img_url
    row["photo_url"] = img_url
    return {"success": True, "message": "Report submitted successfully", "data": row}


@router.put("/{report_id}")
async def update_resident_report(
    report_id: str,
    body: dict,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin", "city_health_officer"))]
):
    """Update report status and optionally forward/escalate to Sanitization Inspector."""
    sb = get_supabase()
    existing = sb.table("resident_reports").select("*").eq("id", report_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Report not found")
    current_report = existing.data[0]
    if current_user["role"] == "barangay_official" and current_report.get("barangay") != current_user.get("barangay"):
        raise HTTPException(status_code=403, detail="You can only process reports in your barangay.")
    aliases = {"completed": "done", "resolved": "done", "processing": "in_progress", "passed_to_chu": "escalated"}
    status = aliases.get(body.get("status"), body.get("status"))
    if body.get("pass_to_inspector"):
        status = "escalated"
    reason = body.get("reason")
    allowed = {"pending", "validated", "escalated", "in_progress", "done", "dismissed", "rejected"}
    if status and status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid report status")
    old_status = aliases.get(current_report.get("status"), current_report.get("status"))
    if old_status in ("done", "dismissed", "rejected") and status and status != old_status:
        raise HTTPException(status_code=409, detail="This report is already closed.")
    transitions = {
        "pending": {"pending", "validated", "escalated", "done", "dismissed", "rejected"},
        "validated": {"validated", "escalated", "done", "dismissed", "rejected"},
        "escalated": {"escalated", "in_progress", "done", "dismissed"},
        "assigned": {"escalated", "in_progress", "done", "dismissed"},
        "in_progress": {"in_progress", "escalated", "done", "dismissed"},
        "done": {"done"}, "dismissed": {"dismissed"}, "rejected": {"rejected"},
    }
    if status and status not in transitions.get(old_status, set()):
        raise HTTPException(status_code=409, detail="This report cannot move back to an earlier processing stage.")
    if current_user["role"] in ("sanitization_inspector", "city_health_officer"):
        if old_status not in ("escalated", "assigned", "in_progress", "done", "dismissed"):
            raise HTTPException(status_code=403, detail="This report has not been forwarded to CHU.")
    if status == "in_progress" and current_user["role"] == "barangay_official":
        raise HTTPException(status_code=403, detail="CHU starts inspection processing.")
    if status == "escalated":
        try:
            result = sb.rpc("forward_resident_report_to_chu", {
                "p_report_id": report_id, "p_actor_id": current_user["id"], "p_reason": reason
            }).execute()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not forward the report to CHU. Check the CHU workflow migration and retry.") from exc
        report = result.data
        if isinstance(report, list):
            report = report[0] if report else None
        if not report:
            raise HTTPException(status_code=500, detail="Forwarding returned no report")
    else:
        update_data = {"actioned_by": current_user["id"]}
        if status:
            update_data["status"] = status
        if reason is not None:
            update_data["reason"] = reason
        result = sb.table("resident_reports").update(update_data).eq("id", report_id).eq("status", current_report["status"]).execute()
        if not result.data:
            raise HTTPException(status_code=409, detail="This report changed on another device. Refresh and try again.")
        report = result.data[0]

    # If completed or resolved (e.g. marked done by brgy or registered into official registry by CHU), notify resident
    if status in ("completed", "resolved", "done"):
        try:
            notif_msg = reason or "Your report has been processed and marked Done. Review the recorded findings for water safety information."
            sb.table("notifications").insert({
                "title": f"Report Processing Completed: {report.get('title') or 'Water Source'}",
                "message": f"Brgy. {report.get('barangay')}: {notif_msg}",
                "type": "report_status",
                "barangay": report.get("barangay"),
                "target_roles": ["resident"],
                "created_by": current_user["id"]
            }).execute()
        except Exception as ex:
            print("Auto-create completed notification failed:", ex)
    elif status == "dismissed":
        try:
            notif_msg = reason or "Your submitted report has been reviewed and dismissed by your Barangay Official."
            sb.table("notifications").insert({
                "title": f"Report Dismissed: {report.get('title') or 'Water Concern'}",
                "message": f"Brgy. {report.get('barangay')}: {notif_msg}",
                "type": "report_status",
                "barangay": report.get("barangay"),
                "target_roles": ["resident"],
                "created_by": current_user["id"]
            }).execute()
        except Exception as ex:
            print("Auto-create dismissed notification failed:", ex)

    return {"success": True, "message": f"Report updated to {status}", "data": report}


@router.put("/{report_id}/done")
async def mark_report_done(
    report_id: str,
    body: Optional[ResidentReportAction] = None,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))] = None
):
    """Barangay official marks a resident report as done."""
    return await update_resident_report(report_id, {"status": "done", "reason": body.reason if body else None}, current_user)


@router.put("/{report_id}/dismiss")
async def mark_report_dismissed(
    report_id: str,
    body: Optional[ResidentReportAction] = None,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))] = None
):
    """Barangay official marks a resident report as dismissed."""
    return await update_resident_report(report_id, {"status": "dismissed", "reason": body.reason if body else None}, current_user)


@router.put("/{report_id}/validate")
async def validate_report(
    report_id: str,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official validates/approves a resident report."""
    return await update_resident_report(report_id, {"status": "validated"}, current_user)


@router.put("/{report_id}/reject")
async def reject_report(
    report_id: str,
    body: ResidentReportAction,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official rejects a resident report."""
    return await update_resident_report(report_id, {"status": "dismissed", "reason": body.reason}, current_user)


@router.put("/{report_id}/escalate")
async def escalate_report(
    report_id: str,
    body: ResidentReportAction,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official escalates a concern directly to CHU."""
    return await update_resident_report(report_id, {"status": "escalated", "reason": body.reason}, current_user)


@router.delete("/{report_id}", include_in_schema=False)
@router.delete("/{report_id}")
async def delete_resident_report(
    report_id: str,
    current_user: Annotated[dict, Depends(require_roles("resident", "admin"))]
):
    """Delete a pending resident report. Only the resident who submitted it (or an admin) can delete it."""
    import re
    sb = get_supabase()

    # Fetch report to check ownership and status
    res = sb.table("resident_reports").select("*").eq("id", report_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Report not found")

    report = res.data[0]

    # Residents can only delete their own reports
    if current_user["role"] == "resident" and str(report.get("submitted_by")) != str(current_user["id"]):
        raise HTTPException(status_code=403, detail="You do not have permission to delete this report")

    # Only pending reports can be deleted (unless admin)
    status = (report.get("status") or "pending").lower()
    if status != "pending" and current_user["role"] != "admin":
        raise HTTPException(status_code=400, detail="Only pending concerns can be deleted")

    # Try to delete associated photo if any
    desc = report.get("description") or ""
    img_url = report.get("image_url") or report.get("photo_url")
    if not img_url:
        match = re.search(r'\[(?:Attached )?Photo Proof:\s*([^\]]+)\]', desc, re.IGNORECASE)
        if match:
            img_url = match.group(1).strip()
    if img_url:
        try:
            await delete_from_supabase(img_url)
        except Exception:
            pass

    # Delete from database
    sb.table("resident_reports").delete().eq("id", report_id).execute()
    return {"success": True, "message": "Report deleted successfully"}
