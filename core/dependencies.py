from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core.security import decode_access_token
from core.database import get_supabase

security_scheme = HTTPBearer()

# ---------------------------------------------------------------------------
# Valid roles (keep in sync with DB check constraint)
# ---------------------------------------------------------------------------
VALID_ROLES = {
    "admin",
    "city_health_officer",
    "sanitization_inspector",
    "barangay_official",
    "resident",
}


# ---------------------------------------------------------------------------
# get_current_user — extracts and validates JWT, returns user dict
# ---------------------------------------------------------------------------
async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
) -> dict:
    """Decode the bearer token and fetch the full user row from Supabase."""
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    sb = get_supabase()
    result = sb.table("users").select("*").eq("id", user_id).single().execute()

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    user = result.data
    if not user.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    return user


# ---------------------------------------------------------------------------
# Role-based guard factories
# ---------------------------------------------------------------------------
def require_roles(*allowed_roles: str):
    """Return a dependency that rejects users whose role is not in *allowed_roles*."""

    async def _guard(
        current_user: Annotated[dict, Depends(get_current_user)],
    ) -> dict:
        if current_user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Required role(s): {', '.join(allowed_roles)}",
            )
        return current_user

    return _guard


# Convenience shortcuts
require_admin = require_roles("admin")
require_admin_or_inspector = require_roles("admin", "sanitization_inspector")
require_admin_cho_inspector = require_roles("admin", "city_health_officer", "sanitization_inspector")
require_staff = require_roles("admin", "city_health_officer", "sanitization_inspector", "barangay_official")
require_any_authenticated = get_current_user
