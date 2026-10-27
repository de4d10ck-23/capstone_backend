"""Account-wide notification read state, stored only in the shared database."""
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set, Tuple

from fastapi import HTTPException
from core.database import get_supabase

logger = logging.getLogger(__name__)
PAGE_SIZE = 500


def get_user_read_state(user_id: str) -> Tuple[Set[str], Optional[datetime]]:
    if not user_id:
        return set(), None
    try:
        sb = get_supabase()
        read_ids = set()
        offset = 0
        while True:
            rows = (sb.table("notification_reads").select("notification_id")
                    .eq("user_id", user_id).order("notification_id")
                    .range(offset, offset + PAGE_SIZE - 1).execute().data or [])
            read_ids.update(str(row["notification_id"]) for row in rows)
            if len(rows) < PAGE_SIZE:
                return read_ids, None
            offset += PAGE_SIZE
    except Exception as exc:
        logger.exception("Could not load shared notification read state")
        raise HTTPException(status_code=503, detail="Unable to load notification read status. Please try again.") from exc


def mark_notification_read(user_id: str, notification_id: str) -> bool:
    return mark_all_notifications_read(user_id, [{"id": notification_id}])


def mark_all_notifications_read(user_id: str, current_notifications: Optional[List[Dict[str, Any]]] = None) -> bool:
    if not user_id:
        raise HTTPException(status_code=400, detail="A user is required to save read status.")
    ids = {str(n["id"]) for n in (current_notifications or []) if n.get("id")}
    if not ids:
        return True
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"user_id": user_id, "notification_id": nid, "read_at": now} for nid in sorted(ids)]
    try:
        sb = get_supabase()
        for offset in range(0, len(rows), PAGE_SIZE):
            sb.table("notification_reads").upsert(
                rows[offset:offset + PAGE_SIZE], on_conflict="user_id,notification_id"
            ).execute()
    except Exception as exc:
        logger.exception("Could not save shared notification read state")
        raise HTTPException(status_code=503, detail="Read status was not fully saved. Please try again.") from exc
    return True


def enrich_notifications_with_read_status(notifications: List[Dict[str, Any]], user_id: Optional[str]) -> List[Dict[str, Any]]:
    if not notifications:
        return []
    read_ids, _ = get_user_read_state(user_id) if user_id else (set(), None)
    return [dict(n, is_read=str(n.get("id")) in read_ids) for n in notifications]
