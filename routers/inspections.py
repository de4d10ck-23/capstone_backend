from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import get_current_user, require_admin, require_roles
from models.inspection import InspectionCreate, InspectionAssign, InspectionComplete
from services.workflow_notifications import notify_staff, notify_linked_resident_outcome

router = APIRouter(prefix="/api/inspections", tags=["Inspections"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_inspections(current_user: Annotated[dict, Depends(get_current_user)]):
    """List inspection requests. Filtered by role."""
    sb = get_supabase()
    query = sb.table("inspection_requests").select("*, resident_report:resident_reports(id,title,type,status,description,reason)").order("created_at", desc=True)

    if current_user["role"] == "sanitization_inspector":
        # Inspectors see all inspection requests in need of sampling or assigned to them
        pass
    elif current_user["role"] == "barangay_official":
        # Officials see requests in their barangay
        if not (current_user.get("barangay") or "").strip():
            raise HTTPException(403, "Your account has no assigned barangay. Contact an administrator.")
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
    
    req_barangay = (current_user.get("barangay") or "").strip()
    if not req_barangay:
        raise HTTPException(403, "Your account has no assigned barangay. Contact an administrator.")
    if not body.location_id:
        raise HTTPException(422, "Select a registered water source before requesting an inspection.")
    sources = (sb.table("water_locations").select("id,barangay,latitude,longitude")
               .eq("id", body.location_id).is_("archived_at", "null").execute().data or [])
    if not sources:
        raise HTTPException(404, "The selected water source is unavailable. Select another source.")
    source = sources[0]
    if (source.get("barangay") or "").strip().lower() != req_barangay.lower():
        raise HTTPException(403, "Select a water source in your assigned barangay.")
    
    new_request = {
        "location_id": body.location_id,
        "description": body.description,
        "latitude": source.get("latitude"),
        "longitude": source.get("longitude"),
        "barangay": source["barangay"],
        "requested_by": current_user["id"],
        "status": "pending"
    }

    result = sb.table("inspection_requests").insert(new_request).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create inspection request")

    await notify_staff(["sanitization_inspector"], "New inspection request",
                       "A new water source inspection request needs action.", "/inspections",
                       f"inspection-{result.data[0]['id']}")
    return {"success": True, "message": "Inspection request submitted", "data": result.data[0]}


@router.put("/{request_id}")
async def update_inspection(
    request_id: str,
    body: dict,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector", "barangay_official", "admin", "city_health_officer"))]
):
    """Update inspection request status, notes, or location."""
    sb = get_supabase()
    existing = sb.table("inspection_requests").select("*").eq("id", request_id).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Inspection request not found")
    current = existing.data[0]
    if current_user["role"] == "barangay_official":
        raise HTTPException(status_code=403, detail="Only CHU can process inspection tickets.")
    if current_user["role"] == "sanitization_inspector" and current.get("assigned_to") not in (None, current_user["id"]):
        raise HTTPException(status_code=409, detail="This inspection is assigned to another inspector.")
    status = body.get("status")
    if status == "done":
        status = "completed"
    if status and status not in ("pending", "assigned", "in_progress", "completed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid inspection status")
    if current.get("status") in ("completed", "cancelled") and status != current.get("status"):
        raise HTTPException(status_code=409, detail="This inspection is already closed.")
    if current.get("status") in ("completed", "cancelled"):
        if any(value is not None and value != current.get(key) for key, value in body.items() if key != "status"):
            raise HTTPException(409, "Closed inspections cannot be edited.")
        return {"success": True, "message": "Inspection is already closed", "data": current}
    if current.get("status") == "in_progress" and status in ("pending", "assigned"):
        raise HTTPException(409, "A processing inspection cannot return to the pending queue.")
    if current.get("status") == "assigned" and status == "pending":
        raise HTTPException(409, "An assigned inspection cannot return to pending.")
    if status == "completed":
        if current.get("status") not in ("in_progress", "completed"):
            raise HTTPException(status_code=409, detail="Start processing before completing this inspection.")
    notes = body.get("notes")
    assigned_to = body.get("assigned_to")
    location_id = body.get("location_id")
    if status == "assigned" and not (assigned_to or current.get("assigned_to")):
        raise HTTPException(422, "Choose an inspector before assigning this inspection.")
    if notes is not None and (not isinstance(notes, str) or len(notes) > 5000):
        raise HTTPException(422, "Inspection notes must be text of at most 5000 characters.")

    if assigned_to:
        if current_user["role"] == "sanitization_inspector" and assigned_to != current_user["id"]:
            raise HTTPException(403, "Only a CHU doctor or administrator can assign another inspector.")
        assignee = sb.table("users").select("role,is_active").eq("id", assigned_to).execute()
        if not assignee.data or assignee.data[0].get("role") != "sanitization_inspector" or not assignee.data[0].get("is_active"):
            raise HTTPException(status_code=400, detail="Choose an active sanitization inspector.")

    update_data = {}
    if status:
        update_data["status"] = "completed" if status in ("completed", "done") else status
    if notes is not None:
        update_data["notes"] = notes
    if location_id:
        sources = (sb.table("water_locations").select("id,barangay,latitude,longitude")
                   .eq("id", location_id).is_("archived_at", "null").execute().data or [])
        if not sources:
            raise HTTPException(404, "The selected water source is unavailable.")
        if (sources[0].get("barangay") or "").strip().lower() != (current.get("barangay") or "").strip().lower():
            raise HTTPException(403, "Select a water source in the inspection's barangay.")
        if current.get("resident_report_id") and location_id != current.get("location_id"):
            raise HTTPException(409, "The source linked to an endorsed concern cannot be replaced.")
        update_data["location_id"] = location_id
        update_data["latitude"] = sources[0].get("latitude")
        update_data["longitude"] = sources[0].get("longitude")
    if assigned_to:
        update_data["assigned_to"] = assigned_to
    elif current_user["role"] == "sanitization_inspector" and (status in ("in_progress", "completed", "done")):
        update_data["assigned_to"] = current_user["id"]

    if not update_data:
        raise HTTPException(400, "No inspection changes were provided.")
    query = sb.table("inspection_requests").update(update_data).eq("id", request_id).eq("status", current["status"])
    # A concurrent reassignment must invalidate an editor's stale request even
    # when the other editor leaves the workflow status unchanged.
    query = query.eq("assigned_to", current["assigned_to"]) if current.get("assigned_to") else query.is_("assigned_to", "null")
    result = query.execute()
    if not result.data:
        raise HTTPException(status_code=409, detail="This inspection changed on another device. Refresh the queue and try again.")

    updated_inspection = result.data[0]

    # The database trigger updates the linked report and its private notification.
    if status in ("completed", "cancelled") and current.get("resident_report_id"):
        await notify_linked_resident_outcome(current["resident_report_id"])

    return {"success": True, "message": "Inspection updated successfully", "data": updated_inspection}


@router.put("/{request_id}/assign")
async def assign_inspection(
    request_id: str,
    body: InspectionAssign,
    current_user: Annotated[dict, Depends(require_roles("admin", "city_health_officer"))]
):
    """Assign an inspector to a request."""
    return await update_inspection(request_id, {"status": "assigned", "assigned_to": body.assigned_to}, current_user)


@router.put("/{request_id}/start")
async def start_inspection(
    request_id: str,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector"))]
):
    """Mark an inspection as in-progress."""
    return await update_inspection(request_id, {"status": "in_progress"}, current_user)


@router.put("/{request_id}/complete")
async def complete_inspection(
    request_id: str,
    body: InspectionComplete,
    current_user: Annotated[dict, Depends(require_roles("sanitization_inspector"))]
):
    """Mark an inspection as completed."""
    return await update_inspection(request_id, {"status": "completed", "notes": body.notes}, current_user)
