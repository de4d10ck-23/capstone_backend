from pydantic import BaseModel, Field
from typing import Optional


class InspectionCreate(BaseModel):
    location_id: Optional[str] = None  # existing water location, or None for new
    description: str = Field(..., min_length=1)
    priority: str = "medium"  # low | medium | high
    latitude: Optional[float] = None  # for new location requests
    longitude: Optional[float] = None
    barangay: Optional[str] = None


class InspectionAssign(BaseModel):
    assigned_to: str  # user id of inspector


class InspectionComplete(BaseModel):
    notes: Optional[str] = None
