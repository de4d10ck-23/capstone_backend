from pydantic import BaseModel, Field
from typing import Optional


class ResidentReportCreate(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    type: Optional[str] = "concern"
    category: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    barangay: Optional[str] = None
    image_url: Optional[str] = None
    photo_url: Optional[str] = None


class ResidentReportAction(BaseModel):
    reason: Optional[str] = None  # for reject / escalate
