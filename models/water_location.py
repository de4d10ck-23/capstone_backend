from pydantic import BaseModel, Field
from typing import Optional


class WaterLocationCreate(BaseModel):
    full_name: str = Field(..., min_length=1)
    barangay: Optional[str] = None
    latitude: float
    longitude: float
    coliform_bacteria: Optional[bool] = None
    e_coli: Optional[bool] = None
    bacteriological_exam: Optional[str] = None  # passed | failed | untested
    image_url: Optional[str] = None
    sample_date: Optional[str] = None
    sample_time: Optional[str] = None
    notes: Optional[str] = None


class WaterLocationUpdate(BaseModel):
    full_name: Optional[str] = None
    barangay: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    coliform_bacteria: Optional[bool] = None
    e_coli: Optional[bool] = None
    bacteriological_exam: Optional[str] = None
    image_url: Optional[str] = None
    sample_date: Optional[str] = None
    sample_time: Optional[str] = None
    status: Optional[str] = None  # pending | reviewed | flagged
    notes: Optional[str] = None
