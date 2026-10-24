import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Set, Tuple

from core.database import get_supabase

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "waterwatch.db")


def _get_connection():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_notification_storage():
    """Ensure SQLite tables for user notification read statuses exist."""
    try:
        with _get_connection() as con:
            cur = con.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_notification_reads (
                    user_id TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    read_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, notification_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_notification_state (
                    user_id TEXT PRIMARY KEY,
                    last_read_all_at TEXT NOT NULL
                );
            """)
            con.commit()
    except Exception as e:
        logger.error(f"Error initializing notification reads storage: {e}")


# Initialize table on import
init_notification_storage()


def _parse_iso(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        cleaned = dt_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


def get_user_read_state(user_id: str) -> Tuple[Set[str], Optional[datetime]]:
    """Return set of read notification IDs and the timestamp when user marked all as read."""
    if not user_id:
        return set(), None

    read_ids: Set[str] = set()
    last_read_all: Optional[datetime] = None

    # 1. Read from SQLite persistent store
    try:
        with _get_connection() as con:
            cur = con.cursor()
            cur.execute("SELECT notification_id FROM user_notification_reads WHERE user_id = ?", (user_id,))
            for row in cur.fetchall():
                read_ids.add(row["notification_id"])

            cur.execute("SELECT last_read_all_at FROM user_notification_state WHERE user_id = ?", (user_id,))
            state_row = cur.fetchone()
            if state_row and state_row["last_read_all_at"]:
                last_read_all = _parse_iso(state_row["last_read_all_at"])
    except Exception as e:
        logger.warning(f"Error querying local notification reads: {e}")

    # 2. Attempt sync with Supabase if table exists
    try:
        sb = get_supabase()
        res = sb.table("notification_reads").select("notification_id").eq("user_id", user_id).execute()
        if res.data:
            for item in res.data:
                read_ids.add(item["notification_id"])
    except Exception:
        pass

    return read_ids, last_read_all


def mark_notification_read(user_id: str, notification_id: str) -> bool:
    """Mark a specific notification as read by user across devices."""
    if not user_id or not notification_id:
        return False

    now_iso = datetime.utcnow().isoformat() + "Z"

    # Save to SQLite
    try:
        with _get_connection() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO user_notification_reads (user_id, notification_id, read_at)
                VALUES (?, ?, ?)
            """, (user_id, notification_id, now_iso))
            con.commit()
    except Exception as e:
        logger.error(f"Error storing notification read locally: {e}")

    # Attempt to sync with Supabase
    try:
        sb = get_supabase()
        sb.table("notification_reads").upsert({
            "user_id": user_id,
            "notification_id": notification_id,
            "read_at": now_iso
        }).execute()
    except Exception:
        pass

    return True


def mark_all_notifications_read(user_id: str, current_notifications: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Mark all notifications as read for this user across all their devices."""
    if not user_id:
        return False

    now_iso = datetime.utcnow().isoformat() + "Z"

    # Save to SQLite
    try:
        with _get_connection() as con:
            cur = con.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO user_notification_state (user_id, last_read_all_at)
                VALUES (?, ?)
            """, (user_id, now_iso))

            if current_notifications:
                for notif in current_notifications:
                    n_id = notif.get("id")
                    if n_id:
                        cur.execute("""
                            INSERT OR IGNORE INTO user_notification_reads (user_id, notification_id, read_at)
                            VALUES (?, ?, ?)
                        """, (user_id, n_id, now_iso))

            con.commit()
    except Exception as e:
        logger.error(f"Error marking all read locally: {e}")

    # Sync to Supabase if available
    try:
        sb = get_supabase()
        if current_notifications:
            rows = [{"user_id": user_id, "notification_id": n["id"], "read_at": now_iso} for n in current_notifications if n.get("id")]
            if rows:
                sb.table("notification_reads").upsert(rows).execute()
    except Exception:
        pass

    return True


def enrich_notifications_with_read_status(
    notifications: List[Dict[str, Any]],
    user_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Enrich notification records with is_read boolean based on user's persistent read state."""
    if not notifications:
        return []

    if not user_id:
        for n in notifications:
            n["is_read"] = False
        return notifications

    read_ids, last_read_all = get_user_read_state(user_id)

    for notif in notifications:
        n_id = notif.get("id")
        created_at_dt = _parse_iso(notif.get("created_at"))

        is_read = False
        if n_id and n_id in read_ids:
            is_read = True
        elif last_read_all and created_at_dt and created_at_dt <= last_read_all:
            is_read = True

        notif["is_read"] = is_read

    return notifications
