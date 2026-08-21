from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import get_current_user, require_admin, require_roles
from models.inspection import InspectionCreate, InspectionAssign, InspectionComplete

router = APIRouter(prefix="/api/inspections", tags=["Inspections"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_inspections(current_user: Annotated[dict, Depends(get_current_user)]):
    """List inspection requests. Filtered by role."""
    sb = get_supabase()
    query = sb.table("inspection_requests").select("*").order("created_at", desc=True)

    if current_user["role"] == "sanitization_inspector":
        # Inspectors see all inspection requests in need of sampling or assigned to them
        pass
    elif current_user["role"] == "barangay_official":
        # Officials see requests in their barangay
        if current_user.get("barangay"):
             query = query.eq("barangay", current_user["barangay"])
    elif current_user["role"] == "resident":
         # Residents see their own requests
         query = query.eq("requested_by", current_user["id"])

    result = query.execute()
    return {"success": True, "data": result.data or []}


@router.post("", include_in_schema=False)
@router.post("/")
async def create_inspection(
    body: InspectionCreate,
    current_user: Annotated[dict, Depends(require_roles("barangay_official", "resident"))]
):
    """Create a new inspection request."""
    sb = get_supabase()
    
    req_barangay = body.barangay or current_user.get("barangay")
    
    new_request = {
        "location_id": body.location_id,
        "description": body.description,
        "priority": body.priority,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "barangay": req_barangay,
        "requested_by": current_user["id"],
        "status": "pending"
    }

    result = sb.table("inspection_requests").insert(new_request).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create inspection request")

    return {"success": True, "message": "Inspection request submitted", "data": result.data[0]}


@router.put("/{request_id}")
async def update_inspection(
    request_id: str,
    body: dict,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector", "barangay_official", "admin", "city_health_officer"))]
):
    """Update inspection request status, notes, or assignment."""
    sb = get_supabase()
    status = body.get("status")
    notes = body.get("notes")
    assigned_to = body.get("assigned_to")

    update_data = {}
    if status:
        update_data["status"] = status
    if notes is not None:
        update_data["notes"] = notes
    if assigned_to:
        update_data["assigned_to"] = assigned_to
    elif current_user["role"] == "sanitization_inspector" and (status == "in_progress" or status == "assigned"):
        update_data["assigned_to"] = current_user["id"]

    result = sb.table("inspection_requests").update(update_data).eq("id", request_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Inspection request not found")

    return {"success": True, "message": "Inspection updated successfully", "data": result.data[0]}


@router.put("/{request_id}/assign")
async def assign_inspection(
    request_id: str,
    body: InspectionAssign,
    current_user: Annotated[dict, Depends(require_roles("admin", "barangay_official", "city_health_officer"))]
):
    """Assign an inspector to a request."""
    sb = get_supabase()
    
    assignee = sb.table("users").select("role").eq("id", body.assigned_to).single().execute()
    if not assignee.data or assignee.data["role"] != "sanitization_inspector":
        raise HTTPException(status_code=400, detail="Assigned user must be a Sanitization Inspector")

    result = sb.table("inspection_requests").update({
        "assigned_to": body.assigned_to,
        "status": "assigned"
    }).eq("id", request_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    return {"success": True, "message": "Inspection assigned", "data": result.data[0]}


@router.put("/{request_id}/start")
async def start_inspection(
    request_id: str,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector"))]
):
    """Mark an inspection as in-progress."""
    sb = get_supabase()
    
    result = sb.table("inspection_requests").update({
        "status": "in_progress",
        "assigned_to": current_user["id"]
    }).eq("id", request_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    return {"success": True, "data": result.data[0]}


@router.put("/{request_id}/complete")
async def complete_inspection(
    request_id: str,
    body: InspectionComplete,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector"))]
):
    """Mark an inspection as completed."""
    sb = get_supabase()
    
    result = sb.table("inspection_requests").update({
        "status": "completed",
        "notes": body.notes,
        "assigned_to": current_user["id"]
    }).eq("id", request_id).execute()
    
    if not result.data:
        raise HTTPException(status_code=404, detail="Request not found")

    return {"success": True, "message": "Inspection completed", "data": result.data[0]}
