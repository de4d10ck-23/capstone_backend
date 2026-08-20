from supabase import create_client, Client
from core.config import settings

# Create a single Supabase client using the service role key.
# This bypasses RLS and gives full table access — used server-side only.
supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


def get_supabase() -> Client:
    """Return the shared Supabase client instance."""
    return supabase
