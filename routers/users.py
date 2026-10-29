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
    _user: Annotated[dict, Depends(require_admin)],
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

    # Check username uniqueness (case-insensitive)
    existing = sb.table("users").select("id").ilike("username", body.username.strip()).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Username already exists. Please choose a different username.")

    # Check email uniqueness (case-insensitive)
    clean_email = body.email.lower()
    if clean_email:
        existing_email = sb.table("users").select("id").ilike("email", clean_email).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail=f"Email address '{body.email.strip()}' is already registered to another account.")

    new_user = {
        "full_name": body.full_name.strip(),
        "username": body.username.strip(),
        "email": clean_email,
        "password_hash": hash_password(body.password),
        "role": body.role,
        "barangay": assigned_barangay,
        "is_active": body.is_active,
        "email_verified": True,
    }

    try:
        result = sb.table("users").insert(new_user).execute()
        if not result.data:
            raise HTTPException(status_code=500, detail="Failed to create user in database")
    except HTTPException:
        raise
    except Exception as e:
        err_str = str(e).lower()
        if "email" in err_str and ("duplicate" in err_str or "unique" in err_str):
            raise HTTPException(status_code=400, detail="Email address is already registered to another account.")
        if "username" in err_str and ("duplicate" in err_str or "unique" in err_str):
            raise HTTPException(status_code=400, detail="Username already exists. Please choose a different username.")
        raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")

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
    result = sb.table("users").select("id, full_name, username, email, role, barangay, is_active, created_at, last_login").eq("id", user_id).limit(1).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "data": result.data[0]}


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

    if "password" in update_data:
        password = update_data.pop("password")
        if not password.strip():
            raise HTTPException(status_code=400, detail="Password cannot be blank")
        update_data["password_hash"] = hash_password(password)

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

    # Check username uniqueness on update
    if "username" in update_data and update_data["username"]:
        clean_user = update_data["username"].strip()
        existing_user = sb.table("users").select("id").ilike("username", clean_user).neq("id", user_id).execute()
        if existing_user.data:
            raise HTTPException(status_code=400, detail="Username already exists. Please choose a different username.")
        update_data["username"] = clean_user

    # Check email uniqueness on update
    if "email" in update_data and update_data["email"]:
        clean_email = update_data["email"].strip().lower()
        existing_email = sb.table("users").select("id").ilike("email", clean_email).neq("id", user_id).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail=f"Email address '{update_data['email']}' is already registered to another account.")
        update_data["email"] = clean_email

    try:
        result = sb.table("users").update(update_data).eq("id", user_id).execute()
    except Exception as e:
        err_str = str(e).lower()
        if "email" in err_str and ("duplicate" in err_str or "unique" in err_str):
            raise HTTPException(status_code=400, detail="Email address is already registered to another account.")
        if "username" in err_str and ("duplicate" in err_str or "unique" in err_str):
            raise HTTPException(status_code=400, detail="Username already exists. Please choose a different username.")
        raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")

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
    current = sb.table("users").select("is_active").eq("id", user_id).limit(1).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="User not found")

    new_active = not current.data[0]["is_active"]
    result = sb.table("users").update({"is_active": new_active}).eq("id", user_id).eq("is_active", current.data[0]["is_active"]).execute()
    if not result.data:
        raise HTTPException(409, "This account changed on another device. Refresh and try again.")

    return {"success": True, "message": f"User {'activated' if new_active else 'deactivated'}", "is_active": new_active}


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    _admin: Annotated[dict, Depends(require_admin)],
):
    """Delete a user. Admin only."""
    sb = get_supabase()
    current = sb.table("users").select("username").eq("id", user_id).limit(1).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="User not found")
    username = current.data[0]["username"]
    if username.strip().casefold() == "admin":
        raise HTTPException(status_code=403, detail="The admin account cannot be deleted.")
    # Require the same username so a concurrent rename cannot bypass protection.
    result = sb.table("users").delete().eq("id", user_id).eq("username", username).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User not found")

    return {"success": True, "message": "User deleted"}
