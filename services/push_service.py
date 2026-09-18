import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

import pywebpush
from py_vapid import Vapid

from core.database import get_supabase

logger = logging.getLogger(__name__)

VAPID_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.abspath(os.path.join(VAPID_DIR, ".."))
KEYS_FILE = os.path.join(BACKEND_DIR, "vapid_keys.json")
LOCAL_SUBS_FILE = os.path.join(BACKEND_DIR, "push_subscriptions.json")

# In-memory subscription cache
_local_subscriptions: Dict[str, Dict[str, Any]] = {}


def _load_or_generate_vapid_keys() -> Dict[str, str]:
    """Load persistent VAPID keys from file or generate a new keypair."""
    # 1. Check environment variables first
    env_public = os.getenv("VAPID_PUBLIC_KEY")
    env_private = os.getenv("VAPID_PRIVATE_KEY")
    if env_public and env_private:
        return {"public_key": env_public, "private_key": env_private}

    # 2. Check saved file
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("public_key") and data.get("private_key"):
                    return data
        except Exception as e:
            logger.warning(f"Failed to read {KEYS_FILE}: {e}")

    # 3. Generate new VAPID keypair
    logger.info("Generating new persistent VAPID keys for WaterWatch...")
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from py_vapid.utils import b64urlencode

    vapid = Vapid()
    vapid.generate_keys()

    raw_pub = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_key_b64 = b64urlencode(raw_pub)
    private_key_pem = vapid.private_pem().decode("utf-8")

    keys = {
        "public_key": public_key_b64,
        "private_key": private_key_pem,
    }

    try:
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump(keys, f, indent=2)
        logger.info(f"Saved new VAPID keys to {KEYS_FILE}")
    except Exception as e:
        logger.error(f"Error saving VAPID keys: {e}")

    return keys


_vapid_keys = _load_or_generate_vapid_keys()

try:
    _vapid_instance = Vapid.from_pem(_vapid_keys["private_key"].encode("utf-8"))
except Exception as e:
    logger.error(f"Failed to initialize Vapid object from PEM: {e}")
    _vapid_instance = None


def get_vapid_public_key() -> str:
    """Return application server public key for frontend push subscription."""
    return _vapid_keys["public_key"]


def _load_local_subscriptions():
    """Load local subscriptions backup file."""
    global _local_subscriptions
    if os.path.exists(LOCAL_SUBS_FILE):
        try:
            with open(LOCAL_SUBS_FILE, "r", encoding="utf-8") as f:
                _local_subscriptions = json.load(f)
        except Exception as e:
            logger.warning(f"Error reading local subscriptions: {e}")


_load_local_subscriptions()


def _save_local_subscriptions():
    """Persist local subscriptions backup to disk."""
    try:
        with open(LOCAL_SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(_local_subscriptions, f, indent=2)
    except Exception as e:
        logger.warning(f"Error saving local subscriptions: {e}")


def save_push_subscription(subscription: Dict[str, Any], user_id: Optional[str] = None, barangay: Optional[str] = None) -> bool:
    """
    Store or update a resident device's push subscription token.
    Saves to Supabase if table exists, with fallback to persistent local store.
    """
    endpoint = subscription.get("endpoint")
    if not endpoint:
        return False

    sub_entry = {
        "endpoint": endpoint,
        "subscription": subscription,
        "user_id": user_id,
        "barangay": (barangay or "").strip().lower() if barangay else None,
        "updated_at": datetime.now().isoformat(),
    }

    # Save to memory & local store
    _local_subscriptions[endpoint] = sub_entry
    _save_local_subscriptions()

    # Attempt to sync with Supabase
    try:
        sb = get_supabase()
        sb.table("push_subscriptions").upsert({
            "endpoint": endpoint,
            "subscription_json": subscription,
            "user_id": user_id,
            "barangay": sub_entry["barangay"],
            "updated_at": sub_entry["updated_at"],
        }, on_conflict="endpoint").execute()
    except Exception as e:
        # Table might not exist yet in Supabase, local storage takes over seamlessly
        logger.info(f"Supabase push_subscriptions sync notice (using local storage): {e}")

    return True


def remove_push_subscription(endpoint: str) -> bool:
    """Remove a push subscription when user opts out or token expires."""
    if not endpoint:
        return False

    if endpoint in _local_subscriptions:
        del _local_subscriptions[endpoint]
        _save_local_subscriptions()

    try:
        sb = get_supabase()
        sb.table("push_subscriptions").delete().eq("endpoint", endpoint).execute()
    except Exception as e:
        logger.debug(f"Supabase delete notice: {e}")

    return True


def get_active_subscriptions(target_barangay: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all active subscriptions matching target barangay (or all if broadcast)."""
    # 1. Try Supabase first
    subs_list = []
    try:
        sb = get_supabase()
        res = sb.table("push_subscriptions").select("*").execute()
        if res.data:
            for item in res.data:
                subs_list.append({
                    "endpoint": item.get("endpoint"),
                    "subscription": item.get("subscription_json"),
                    "barangay": (item.get("barangay") or "").strip().lower() if item.get("barangay") else None,
                })
    except Exception as e:
        logger.debug(f"Reading from local subscriptions fallback: {e}")

    # If Supabase returned empty, use local cache
    if not subs_list:
        subs_list = list(_local_subscriptions.values())

    # Filter by target barangay if specified
    if not target_barangay or target_barangay.lower() in ["all", "city-wide", "all barangays"]:
        return subs_list

    norm_target = target_barangay.strip().lower()
    filtered = [
        s for s in subs_list
        if not s.get("barangay") or s.get("barangay") == norm_target
    ]
    return filtered


async def broadcast_push_notification(
    title: str,
    message: str,
    barangay: Optional[str] = None,
    url: str = "/portal/notifications",
    tag: str = "waterwatch-alert",
) -> Dict[str, Any]:
    """
    Dispatch Web Push notification to all registered resident devices.
    Runs concurrently and removes expired subscriptions automatically.
    """
    subscriptions = get_active_subscriptions(barangay)
    if not subscriptions:
        logger.info("No push subscriptions found for broadcast.")
        return {"total": 0, "sent": 0, "failed": 0}

    payload = json.dumps({
        "title": title,
        "body": message,
        "url": url,
        "tag": tag,
        "timestamp": datetime.now().isoformat(),
        "barangay": barangay,
    })

    sent_count = 0
    failed_count = 0

    vapid_claims = {
        "sub": "mailto:waterwatch.maasin@gmail.com"
    }

    for sub in subscriptions:
        sub_info = sub.get("subscription")
        endpoint = sub.get("endpoint")
        if not sub_info:
            continue

        try:
            pywebpush.webpush(
                subscription_info=sub_info,
                data=payload,
                vapid_private_key=_vapid_instance or _vapid_keys["private_key"],
                vapid_claims=vapid_claims,
            )
            sent_count += 1
        except pywebpush.WebPushException as ex:
            failed_count += 1
            logger.warning(f"WebPush error for {endpoint[:30]}...: {ex}")
            # If subscription expired (404/410), clean it up
            if ex.response is not None and ex.response.status_code in [404, 410]:
                remove_push_subscription(endpoint)
        except Exception as e:
            failed_count += 1
            logger.error(f"Unexpected error pushing notification: {e}")

    logger.info(f"Push broadcast completed: {sent_count} sent, {failed_count} failed out of {len(subscriptions)} devices.")
    return {
        "total": len(subscriptions),
        "sent": sent_count,
        "failed": failed_count,
    }
