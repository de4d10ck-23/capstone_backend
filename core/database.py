import os
import logging
from supabase import create_client, Client
from core.config import settings

logger = logging.getLogger(__name__)

_supabase_client: Client | None = None


def get_supabase() -> Client:
    """Return the shared Supabase client instance (lazy-loaded)."""
    global _supabase_client
    if _supabase_client is None:
        url = settings.SUPABASE_URL
        key = settings.SUPABASE_SERVICE_ROLE_KEY

        if not url or not key:
            logger.error("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY environment variable is missing!")
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be provided via environment variables."
            )

        _supabase_client = create_client(url, key)

    return _supabase_client
