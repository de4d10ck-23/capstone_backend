from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status, Depends

from core.database import get_supabase
from core.security import hash_password, verify_password, create_access_token
from core.dependencies import get_current_user
from models.user import LoginRequest, RegisterRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.post("/login")
async def login(body: LoginRequest):
    """Authenticate user with username + password. Returns JWT."""
    sb = get_supabase()

    result = sb.table("users").select("*").eq("username", body.username).execute()

    if not result.data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    user = result.data[0]

    # Check password
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong password")

    # Check active
    if not user.get("is_active", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is deactivated. Contact the administrator.")

    # Update last_login
    sb.table("users").update({"last_login": datetime.now(timezone.utc).isoformat()}).eq("id", user["id"]).execute()

    # Build token
    token = create_access_token({"sub": user["id"], "username": user["username"], "role": user["role"]})

    # Strip password_hash from response
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}

    return {"success": True, "token": token, "user": safe_user}


@router.post("/register")
async def register(body: RegisterRequest):
    """Self-registration for residents only."""
    sb = get_supabase()

    # Check username uniqueness
    existing = sb.table("users").select("id").eq("username", body.username).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Username already exists")

    # Check email uniqueness (if provided)
    if body.email:
        existing_email = sb.table("users").select("id").eq("email", body.email).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail="Email already registered")

    # Create user as resident
    new_user = {
        "full_name": body.full_name.strip(),
        "username": body.username.strip(),
        "email": body.email,
        "password_hash": hash_password(body.password),
        "role": "resident",
        "barangay": body.barangay,
        "is_active": True,
    }

    result = sb.table("users").insert(new_user).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create account")

    user = result.data[0]
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}

    return {"success": True, "message": "Account created successfully", "user": safe_user}


@router.get("/me")
async def get_me(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return current authenticated user's info."""
    safe_user = {k: v for k, v in current_user.items() if k != "password_hash"}
    return {"success": True, "user": safe_user}
