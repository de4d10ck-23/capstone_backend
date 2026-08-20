from pydantic import BaseModel, Field
from typing import Optional


class ResidentReportCreate(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    type: str = "concern"  # concern | new_water_source
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    barangay: Optional[str] = None


class ResidentReportAction(BaseModel):
    reason: Optional[str] = None  # for reject / escalate
