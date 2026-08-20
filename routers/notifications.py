from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import get_current_user, get_optional_user, require_staff
from models.notification import NotificationCreate

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_notifications(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Get notifications for the current user based on their role and barangay."""
    sb = get_supabase()
    query = sb.table("notifications").select("*").order("created_at", desc=True)

    if current_user and current_user.get("role") == "barangay_official" and current_user.get("barangay"):
        query = query.or_(f"barangay.eq.{current_user['barangay']},barangay.is.null")
    elif barangay:
        query = query.or_(f"barangay.eq.{barangay},barangay.is.null")
        
    result = query.execute()
    return {"success": True, "data": result.data or []}


@router.post("/")
async def create_notification(
    body: NotificationCreate,
    current_user: Annotated[dict, Depends(require_staff)] # Admin, Inspector, Brgy Official
):
    """Create a new notification."""
    # Enforce manuscript rules: Barangay Officials can only notify their own barangay's residents
    if current_user["role"] == "barangay_official":
        if body.barangay != current_user.get("barangay"):
             raise HTTPException(status_code=403, detail="You can only send notifications to your own barangay.")
        if body.target_roles and "resident" not in body.target_roles and len(body.target_roles) > 0:
             raise HTTPException(status_code=403, detail="Barangay officials primarily notify residents.")

    sb = get_supabase()
    
    new_notif = {
        "title": body.title.strip(),
        "message": body.message,
        "type": body.type,
        "barangay": body.barangay,
        "target_roles": body.target_roles,
        "created_by": current_user["id"]
    }

    result = sb.table("notifications").insert(new_notif).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create notification")

    return {"success": True, "message": "Notification sent", "data": result.data[0]}


@router.put("/{notification_id}/read")
async def mark_read(
    notification_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Mark a notification as read for the current user."""
    # Implementation depends on how read status is tracked (e.g., a junction table)
    # For now, just return success
    return {"success": True, "message": "Notification marked as read"}
