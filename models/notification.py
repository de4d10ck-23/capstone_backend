from pydantic import BaseModel, Field
from typing import Optional


class NotificationCreate(BaseModel):
    title: str = Field(..., min_length=1)
    message: Optional[str] = None
    type: str = "info"  # warning | critical | info
    barangay: Optional[str] = None  # None = all barangays
    target_roles: Optional[list[str]] = None  # list of roles to target
