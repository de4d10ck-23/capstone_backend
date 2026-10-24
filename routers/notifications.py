import logging
from typing import Annotated
from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import get_current_user, get_optional_user, require_staff
from models.notification import NotificationCreate
from services.push_service import (
    get_vapid_public_key,
    save_push_subscription,
    remove_push_subscription,
    broadcast_push_notification,
)
from services.notification_service import (
    mark_notification_read,
    mark_all_notifications_read,
    enrich_notifications_with_read_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


class PushSubscriptionRequest(BaseModel):
    subscription: dict
    user_id: str | None = None
    barangay: str | None = None


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.get("/vapid-public-key")
async def get_vapid_key():
    """Retrieve VAPID public key for frontend push subscription."""
    return {"success": True, "publicKey": get_vapid_public_key()}


@router.post("/subscribe")
async def subscribe_device(body: PushSubscriptionRequest):
    """Save a resident's push subscription token."""
    saved = save_push_subscription(
        subscription=body.subscription,
        user_id=body.user_id,
        barangay=body.barangay,
    )
    if not saved:
        raise HTTPException(status_code=400, detail="Invalid push subscription payload")
    return {"success": True, "message": "Device successfully registered for push alerts"}


@router.post("/unsubscribe")
async def unsubscribe_device(body: PushUnsubscribeRequest):
    """Remove a resident's push subscription token."""
    remove_push_subscription(body.endpoint)
    return {"success": True, "message": "Device unsubscribed from push alerts"}


@router.post("/test-push")
async def send_test_push(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Send an immediate test push notification to all subscribed devices."""
    res = await broadcast_push_notification(
        title="🔔 WaterWatch Alert Test",
        message="Your device is successfully connected to Maasin City real-time water safety alerts!",
        barangay=barangay,
        url="/portal/notifications",
        tag="test-push-notification",
    )
    return {"success": True, "message": "Test push broadcast dispatched", "details": res}


@router.get("", include_in_schema=False)
@router.get("/")
async def list_notifications(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Get notifications for the current user based on their role and barangay, enriched with persistent read status."""
    sb = get_supabase()
    query = sb.table("notifications").select("*").order("created_at", desc=True)

    if current_user and current_user.get("role") in ("barangay_official", "resident") and current_user.get("barangay"):
        query = query.or_(f"barangay.eq.{current_user['barangay']},barangay.is.null")
    elif barangay:
        query = query.or_(f"barangay.eq.{barangay},barangay.is.null")
        
    result = query.execute()
    raw_data = result.data or []

    # Enrich each notification with persistent is_read status
    user_id = current_user.get("id") if current_user else None
    enriched = enrich_notifications_with_read_status(raw_data, user_id)
    unread_count = sum(1 for n in enriched if not n.get("is_read"))

    return {
        "success": True,
        "data": enriched,
        "unread_count": unread_count,
        "total_count": len(enriched)
    }


@router.get("/unread-count", include_in_schema=False)
@router.get("/unread-count/")
async def get_unread_count(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Retrieve exact unread notification count for synchronization across mobile and web headers."""
    if not current_user:
        return {"success": True, "unread_count": 0}

    sb = get_supabase()
    query = sb.table("notifications").select("id,created_at,barangay")
    if current_user.get("role") in ("barangay_official", "resident") and current_user.get("barangay"):
        query = query.or_(f"barangay.eq.{current_user['barangay']},barangay.is.null")
    elif barangay:
        query = query.or_(f"barangay.eq.{barangay},barangay.is.null")

    result = query.execute()
    enriched = enrich_notifications_with_read_status(result.data or [], current_user.get("id"))
    unread_count = sum(1 for n in enriched if not n.get("is_read"))
    return {"success": True, "unread_count": unread_count}


@router.post("", include_in_schema=False)
@router.post("/")
async def create_notification(
    body: NotificationCreate,
    current_user: Annotated[dict, Depends(require_staff)] # Admin, Inspector, Brgy Official
):
    """Create a new notification and automatically dispatch Web Push to residents."""
    # Enforce manuscript rules: Barangay Officials can only notify their own barangay's residents or send critical escalations to officials
    if current_user["role"] == "barangay_official":
        if body.barangay and body.barangay != current_user.get("barangay"):
             raise HTTPException(status_code=403, detail="You can only send notifications to your own barangay.")
        if body.target_roles and "resident" not in body.target_roles and len(body.target_roles) > 0 and body.type != "critical":
             raise HTTPException(status_code=403, detail="Barangay officials primarily notify residents unless submitting a critical escalation.")


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

    created_record = result.data[0]

    # Automatically broadcast Web Push alerts to registered resident devices
    try:
        await broadcast_push_notification(
            title=f"WaterWatch: {new_notif['title']}",
            message=new_notif.get("message") or "New official water safety advisory has been posted.",
            barangay=new_notif.get("barangay"),
            url="/portal/notifications",
            tag=f"advisory-{created_record.get('id', 'new')}",
        )
    except Exception as push_err:
        logger.warning(f"Error dispatching Web Push alerts: {push_err}")

    return {"success": True, "message": "Notification sent and pushed to residents", "data": created_record}


@router.put("/read-all", include_in_schema=False)
@router.put("/read-all/")
async def mark_all_read(
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Mark all notifications as read for current user in database (syncs across phone and PC)."""
    sb = get_supabase()
    query = sb.table("notifications").select("id,created_at")
    if current_user.get("role") in ("barangay_official", "resident") and current_user.get("barangay"):
        query = query.or_(f"barangay.eq.{current_user['barangay']},barangay.is.null")
    result = query.execute()

    mark_all_notifications_read(current_user["id"], result.data or [])
    return {"success": True, "message": "All notifications marked as read across all devices", "unread_count": 0}


@router.put("/{notification_id}/read", include_in_schema=False)
@router.put("/{notification_id}/read/")
async def mark_read(
    notification_id: str,
    current_user: Annotated[dict, Depends(get_current_user)]
):
    """Mark a notification as read for current user in database (syncs across phone and PC)."""
    mark_notification_read(current_user["id"], notification_id)
    return {"success": True, "message": "Notification marked as read across all devices"}


@router.delete("/{notification_id}", include_in_schema=False)
@router.delete("/{notification_id}/")
async def delete_notification(
    notification_id: str,
    current_user: Annotated[dict, Depends(require_staff)]
):
    """Delete a notification broadcast."""
    sb = get_supabase()
    result = sb.table("notifications").delete().eq("id", notification_id).execute()
    return {"success": True, "message": "Notification deleted"}
