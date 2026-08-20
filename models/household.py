from pydantic import BaseModel
from typing import Optional


class HouseholdResponse(BaseModel):
    longitude: float
    latitude: float
    toilet_facility: Optional[int] = None
    barangay_code: Optional[str] = None
    household_count: Optional[int] = None
