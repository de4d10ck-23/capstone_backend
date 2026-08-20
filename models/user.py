from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2)
    username: str = Field(..., min_length=3)
    email: Optional[str] = None
    password: str = Field(..., min_length=6)
    barangay: Optional[str] = None


class TokenResponse(BaseModel):
    success: bool = True
    token: str
    user: dict


# ---------------------------------------------------------------------------
# User CRUD schemas
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    full_name: str = Field(..., min_length=2)
    username: str = Field(..., min_length=3)
    email: Optional[str] = None
    password: str = Field(..., min_length=6)
    role: str  # admin | city_health_officer | sanitization_inspector | barangay_official | resident
    barangay: Optional[str] = None
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    barangay: Optional[str] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: str
    full_name: str
    username: str
    email: Optional[str] = None
    role: str
    barangay: Optional[str] = None
    is_active: bool
    created_at: Optional[str] = None
    last_login: Optional[str] = None
