from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status, Depends, Request

from core.database import get_supabase
from core.security import hash_password, verify_password, create_access_token
from core.dependencies import get_current_user
from core.constants import validate_and_normalize_barangay
from models.user import LoginRequest, RegisterRequest, TokenResponse, ResidentProfileUpdate, EmailRequest, EmailTokenRequest, ResetPasswordRequest

from services.email_auth_service import issue_email_link, complete_email_action

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

    if not user.get("email_verified", True):
        raise HTTPException(403, "Please verify your email before signing in.")

    # Update last_login
    sb.table("users").update({"last_login": datetime.now(timezone.utc).isoformat()}).eq("id", user["id"]).execute()

    # Build token
    token = create_access_token({"sub": user["id"], "username": user["username"], "role": user["role"], "auth_version": user.get("auth_version", 0)})

    # Strip password_hash from response
    safe_user = {k: v for k, v in user.items() if k != "password_hash"}

    return {"success": True, "token": token, "user": safe_user}


@router.post("/register")
async def register(body: RegisterRequest, request: Request):
    """Self-registration for residents only."""
    sb = get_supabase()

    body.email = str(body.email).strip().lower()
    body.username = body.username.strip()
    if not body.password.strip() or len(body.password.encode("utf-8")) > 72:
        raise HTTPException(400, "Password must be nonblank and at most 72 UTF-8 bytes.")
    # Check username uniqueness
    existing = sb.table("users").select("id").eq("username", body.username).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Username already exists")

    # Required email is normalized before storing or sending.
    if body.email:
        existing_email = sb.table("users").select("id").ilike("email", body.email.replace("%", "\\%").replace("_", "\\_")).execute()
        if existing_email.data:
            raise HTTPException(status_code=400, detail="Email already registered")

    # Validate barangay
    try:
        normalized_barangay = validate_and_normalize_barangay(body.barangay)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Store only a pending registration; confirmation creates the resident row.
    new_user = {
        "full_name": body.full_name.strip(),
        "username": body.username.strip(),
        "email": body.email,
        "password_hash": hash_password(body.password),
        "role": "resident",
        "barangay": normalized_barangay,
        "is_active": True,
    }

    await issue_email_link(body.email, "register", new_user, request.client.host if request.client else "unknown")
    return {"success": True, "message": "Check your email to confirm your account. The link expires in 15 minutes."}


@router.post("/verify-email")
async def verify_email(body: EmailTokenRequest):
    complete_email_action(body.token, "register")
    return {"success": True, "message": "Email verified. Your account is ready. You can now sign in."}


@router.post("/forgot-password")
async def forgot_password(body: EmailRequest, request: Request):
    email = str(body.email).strip().lower()
    pattern = email.replace("%", "\\%").replace("_", "\\_")
    users = get_supabase().table("users").select("id,role,is_active,email_verified,auth_version").ilike("email", pattern).execute().data or []
    user = users[0] if len(users) == 1 else None
    eligible = bool(user and user["role"] == "resident" and user["is_active"] and user.get("email_verified", True))
    payload = {"user_id": user["id"], "auth_version": user.get("auth_version", 0)} if eligible else {}
    await issue_email_link(email, "reset", payload, request.client.host if request.client else "unknown", deliver=eligible)
    return {"success": True, "message": "If an active resident account uses that email, a reset link will arrive shortly. Check spam too. You may request another link after one minute."}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    if not body.password.strip() or len(body.password.encode("utf-8")) > 72:
        raise HTTPException(400, "Password must be nonblank and at most 72 UTF-8 bytes.")
    complete_email_action(body.token, "reset", hash_password(body.password))
    return {"success": True, "message": "Password reset. Please sign in with your new password."}


@router.get("/me")
async def get_me(current_user: Annotated[dict, Depends(get_current_user)]):
    """Return current authenticated user's info."""
    safe_user = {k: v for k, v in current_user.items() if k != "password_hash"}
    return {"success": True, "user": safe_user}


@router.put("/me")
async def update_resident_profile(
    body: ResidentProfileUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Residents may edit only their own name, username and password."""
    if current_user.get("role") != "resident":
        raise HTTPException(status_code=403, detail="This profile endpoint is for residents only")
    sb = get_supabase()
    # Escape LIKE wildcards so existing usernames containing them are matched literally.
    pattern = body.username.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    existing = sb.table("users").select("id").ilike("username", pattern).neq("id", current_user["id"]).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail="Username already exists. Please choose another.")
    changes = {"full_name": body.full_name, "username": body.username}
    if body.new_password is not None:
        if not body.current_password or not verify_password(body.current_password, current_user["password_hash"]):
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        if not body.new_password.strip():
            raise HTTPException(status_code=400, detail="New password cannot be blank")
        changes["password_hash"] = hash_password(body.new_password)
    try:
        result = sb.table("users").update(changes).eq("id", current_user["id"]).execute()
    except Exception as exc:
        if getattr(exc, "code", None) == "23505":
            raise HTTPException(status_code=409, detail="Username already exists. Please choose another.") from exc
        raise HTTPException(status_code=503, detail="Could not save your profile. Please try again.") from exc
    if not result.data:
        raise HTTPException(status_code=404, detail="Account not found")
    safe_user = {k: v for k, v in result.data[0].items() if k != "password_hash"}
    return {"success": True, "user": safe_user}
