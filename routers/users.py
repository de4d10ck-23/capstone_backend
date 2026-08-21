from typing import Annotated, Optional

from fastapi import APIRouter, HTTPException, Depends, Query

from core.database import get_supabase
from core.security import hash_password
from core.dependencies import require_admin, require_staff, VALID_ROLES
from core.constants import validate_and_normalize_barangay
from models.user import UserCreate, UserUpdate

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_users(
    _user: Annotated[dict, Depends(require_staff)],
    role: Optional[str] = Query(None),
):
    """List all users. Optionally filter by role. Admin only."""
    sb = get_supabase()
    query = sb.table("users").select("id, full_name, username, email, role, barangay, is_active, created_at, last_login")

    if role:
        query = query.eq("role", role)

    result = query.order("created_at", desc=True).execute()
    return {"success": True, "data": result.data or []}


@router.post("", include_in_schema=False)
@router.post("/")
async def create_user(
    body: UserCreate,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Create a new user (any role). Admin only."""
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

    # Validate barangay if role is resident or barangay_official
    assigned_barangay = None
    if body.role in ("resident", "barangay_official"):
        try:
            assigned_barangay = validate_and_normalize_barangay(body.barangay)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    sb = get_supabase()

    # Check username uniqueness
    existing = sb.table("users").select("id").eq("username", body.username).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Username already exists")

    # Check email uniqueness
    if body.email:
        existing_email = sb.table("users").select("id").eq("email", body.email).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail="Email already registered")

    new_user = {
        "full_name": body.full_name.strip(),
        "username": body.username.strip(),
        "email": body.email,
        "password_hash": hash_password(body.password),
        "role": body.role,
        "barangay": assigned_barangay,
        "is_active": body.is_active,
    }

    result = sb.table("users").insert(new_user).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create user")

    user = result.data[0]
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}
    return {"success": True, "message": "User created successfully", "user": safe_user}


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Get user details. Admin only."""
    sb = get_supabase()
    result = sb.table("users").select("id, full_name, username, email, role, barangay, is_active, created_at, last_login").eq("id", user_id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "data": result.data}


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Update a user. Admin only."""
    sb = get_supabase()

    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    if "role" in update_data and update_data["role"] not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {', '.join(VALID_ROLES)}")

    # Validate and normalize barangay
    if "barangay" in update_data:
        if update_data["barangay"]:
            try:
                update_data["barangay"] = validate_and_normalize_barangay(update_data["barangay"])
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            update_data["barangay"] = None

    result = sb.table("users").update(update_data).eq("id", user_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    user = result.data[0]
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}
    return {"success": True, "message": "User updated", "user": safe_user}


@router.put("/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: str,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Toggle user active/inactive. Admin only."""
    sb = get_supabase()

    # Fetch current state
    current = sb.table("users").select("is_active").eq("id", user_id).single().execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="User not found")

    new_active = not current.data["is_active"]
    sb.table("users").update({"is_active": new_active}).eq("id", user_id).execute()

    return {"success": True, "message": f"User {'activated' if new_active else 'deactivated'}", "is_active": new_active}


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Delete a user. Admin only."""
    sb = get_supabase()
    result = sb.table("users").delete().eq("id", user_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "User deleted"}
