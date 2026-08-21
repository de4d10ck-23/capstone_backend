from pydantic import BaseModel, Field, ConfigDict
from typing import Optional


class WaterLocationCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: str = Field(..., min_length=1)
    name: Optional[str] = None
    barangay: Optional[str] = None
    latitude: float
    longitude: float
    source_type: Optional[str] = None
    type: Optional[str] = None
    status: Optional[str] = None  # safe | warning | undrinkable | pending
    coliform_bacteria: Optional[bool] = None
    e_coli: Optional[bool] = None
    coliform_count: Optional[int] = None
    e_coli_count: Optional[int] = None
    bacteriological_exam: Optional[str] = None  # passed | failed | untested
    image_url: Optional[str] = None
    sample_date: Optional[str] = None
    sample_time: Optional[str] = None
    notes: Optional[str] = None
    description: Optional[str] = None
    remarks: Optional[str] = None


class WaterLocationUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_name: Optional[str] = None
    name: Optional[str] = None
    barangay: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    source_type: Optional[str] = None
    type: Optional[str] = None
    coliform_bacteria: Optional[bool] = None
    e_coli: Optional[bool] = None
    coliform_count: Optional[int] = None
    e_coli_count: Optional[int] = None
    bacteriological_exam: Optional[str] = None
    image_url: Optional[str] = None
    sample_date: Optional[str] = None
    sample_time: Optional[str] = None
    status: Optional[str] = None  # pending | reviewed | flagged | safe | warning | undrinkable
    notes: Optional[str] = None
    description: Optional[str] = None
    remarks: Optional[str] = None
