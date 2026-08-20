from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import get_current_user, require_roles
from models.resident_report import ResidentReportCreate, ResidentReportAction

router = APIRouter(prefix="/api/resident-reports", tags=["Resident Reports"])


@router.get("/")
async def list_resident_reports(
    current_user: Annotated[dict, Depends(require_roles("admin", "barangay_official"))]
):
    """List resident-submitted reports. Barangay officials see their own barangay only."""
    sb = get_supabase()
    query = sb.table("resident_reports").select("*").order("created_at", desc=True)

    if current_user["role"] == "barangay_official" and current_user.get("barangay"):
         query = query.eq("barangay", current_user["barangay"])

    result = query.execute()
    return {"success": True, "data": result.data or []}


@router.post("/")
async def submit_resident_report(
    body: ResidentReportCreate,
    current_user: Annotated[dict, Depends(require_roles("resident"))]
):
    """Residents submit a concern or new water source."""
    sb = get_supabase()
    
    new_report = {
        "title": body.title.strip(),
        "description": body.description,
        "type": body.type,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "barangay": body.barangay or current_user.get("barangay"),
        "submitted_by": current_user["id"],
        "status": "pending"
    }

    result = sb.table("resident_reports").insert(new_report).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to submit report")

    return {"success": True, "message": "Report submitted successfully", "data": result.data[0]}


@router.put("/{report_id}/validate")
async def validate_report(
    report_id: str,
    current_user: Annotated[dict, Depends(require_roles("barangay_official"))]
):
    """Barangay official validates/approves a resident report."""
    sb = get_supabase()
    
    # Optional: check if report belongs to official's barangay
    
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
    current_user: Annotated[dict, Depends(require_roles("barangay_official"))]
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
    current_user: Annotated[dict, Depends(require_roles("barangay_official"))]
):
    """Barangay official escalates a concern to Admin/Inspector."""
    sb = get_supabase()
    
    result = sb.table("resident_reports").update({
        "status": "escalated",
        "reason": body.reason,
        "actioned_by": current_user["id"]
    }).eq("id", report_id).execute()
    
    if not result.data:
         raise HTTPException(status_code=404, detail="Report not found")
         
    return {"success": True, "message": "Report escalated", "data": result.data[0]}
