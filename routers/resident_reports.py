from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, require_roles
from core.storage import upload_to_supabase
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

    if current_user["role"] == "barangay_official" and current_user.get("barangay"):
        query = query.eq("barangay", current_user["barangay"])
    elif current_user["role"] == "resident":
        query = query.eq("submitted_by", current_user["id"])
    elif current_user["role"] in ("sanitization_inspector", "city_health_officer"):
        # Sanitization inspector & CHU ONLY see reports that have been endorsed/escalated by barangay officials
        query = query.in_("status", ["escalated", "passed_to_chu", "in_progress", "completed", "resolved"])

    result = query.execute()
    data = result.data or []

    # Ensure image_url is populated even if stored in description fallback
    for item in data:
        desc = item.get("description") or ""
        if not item.get("image_url") and not item.get("photo_url"):
            match = re.search(r'\[Attached Photo Proof:\s*(https?://[^\s\]]+)\]', desc, re.IGNORECASE)
            if not match:
                match = re.search(r'\[Photo Proof:\s*(https?://[^\s\]]+)\]', desc, re.IGNORECASE)
            if match:
                item["image_url"] = match.group(1)
                item["photo_url"] = match.group(1)
        
        # Clean description for display
        item["clean_description"] = re.sub(r'\[(?:Attached )?Photo Proof:\s*https?://[^\s\]]+\]', '', desc, flags=re.IGNORECASE).strip()

    return {"success": True, "data": data}


@router.post("", include_in_schema=False)
@router.post("/")
async def submit_resident_report(
    body: ResidentReportCreate,
    current_user: Annotated[dict, Depends(require_roles("resident"))]
):
    """Residents submit a concern or new water source."""
    sb = get_supabase()
    
    report_type = body.category or body.type or "concern"
    img_url = body.image_url or body.photo_url
    
    desc = body.description.strip()
    if img_url and "[Attached Photo Proof:" not in desc:
        desc = f"{desc}\n\n[Attached Photo Proof: {img_url}]"

    new_report = {
        "title": body.title.strip(),
        "description": desc,
        "type": report_type,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "barangay": body.barangay or current_user.get("barangay"),
        "submitted_by": current_user["id"],
        "status": "pending"
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
    status = body.get("status")
    reason = body.get("reason")
    pass_to_inspector = body.get("pass_to_inspector", False)
    
    update_data = {
        "actioned_by": current_user["id"]
    }
    if status:
        update_data["status"] = status
    if reason is not None:
        update_data["reason"] = reason

    result = sb.table("resident_reports").update(update_data).eq("id", report_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Report not found")
    
    report = result.data[0]

    # If forwarded or escalated to CHU (Sanitization Inspector), auto-create inspection request
    if pass_to_inspector or status == "escalated":
        category_label = str(report.get("type") or "Concern").replace("_", " ").title()
        proof_note = f" (Photo Proof attached)" if report.get("image_url") else ""
        inspection_payload = {
            "description": f"[{category_label} passed to CHU by Brgy Official] {report.get('title')}: {report.get('description')}{proof_note}",
            "priority": "high",
            "barangay": report.get("barangay"),
            "latitude": report.get("latitude") or 10.1330,
            "longitude": report.get("longitude") or 124.8700,
            "requested_by": report.get("submitted_by") or current_user["id"],
            "status": "pending"
        }
        try:
            sb.table("inspection_requests").insert(inspection_payload).execute()
        except Exception as ex:
            print("Auto-create inspection failed:", ex)

    # If completed or resolved (e.g. registered into official registry by CHU), notify resident
    if status in ("completed", "resolved"):
        try:
            notif_msg = reason or "Your submitted water point has been officially validated and registered by CHU."
            sb.table("notifications").insert({
                "title": f"Water Concern Resolved: {report.get('title') or 'Water Source'}",
                "message": f"Brgy. {report.get('barangay')}: {notif_msg}",
                "type": "report_status",
                "barangay": report.get("barangay"),
                "target_roles": ["resident"],
                "created_by": current_user["id"]
            }).execute()
        except Exception as ex:
            print("Auto-create completed notification failed:", ex)

    return {"success": True, "message": f"Report updated to {status}", "data": report}


@router.put("/{report_id}/validate")
async def validate_report(
    report_id: str,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official validates/approves a resident report."""
    sb = get_supabase()
    
    result = sb.table("resident_reports").update({
        "status": "validated",
        "actioned_by": current_user["id"]
    }).eq("id", report_id).execute()
    
    if not result.data:
         raise HTTPException(status_code=404, detail="Report not found")
         
    return {"success": True, "message": "Report validated", "data": result.data[0]}


@router.put("/{report_id}/reject")
async def reject_report(
    report_id: str,
    body: ResidentReportAction,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official rejects a resident report."""
    sb = get_supabase()
    
    result = sb.table("resident_reports").update({
        "status": "rejected",
        "reason": body.reason,
        "actioned_by": current_user["id"]
    }).eq("id", report_id).execute()
    
    if not result.data:
         raise HTTPException(status_code=404, detail="Report not found")
         
    return {"success": True, "message": "Report rejected", "data": result.data[0]}


@router.put("/{report_id}/escalate")
async def escalate_report(
    report_id: str,
    body: ResidentReportAction,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "sanitization_inspector", "admin"))]
):
    """Barangay official escalates a concern directly to CHU."""
    sb = get_supabase()
    
    result = sb.table("resident_reports").update({
        "status": "escalated",
        "reason": body.reason,
        "actioned_by": current_user["id"]
    }).eq("id", report_id).execute()
    
    if not result.data:
         raise HTTPException(status_code=404, detail="Report not found")
    
    report = result.data[0]
    category_label = str(report.get("type") or "Concern").replace("_", " ").title()
    proof_note = f" (Photo Proof attached)" if report.get("image_url") else ""
    inspection_payload = {
        "description": f"[{category_label} passed to CHU by Brgy Official] {report.get('title')}: {report.get('description')}{proof_note}",
        "priority": "high",
        "barangay": report.get("barangay"),
        "latitude": report.get("latitude") or 10.1330,
        "longitude": report.get("longitude") or 124.8700,
        "requested_by": report.get("submitted_by") or current_user["id"],
        "status": "pending"
    }
    try:
        sb.table("inspection_requests").insert(inspection_payload).execute()
    except Exception as ex:
        print("Auto-create inspection failed:", ex)
         
    return {"success": True, "message": "Report escalated to CHU", "data": report}
