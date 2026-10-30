from pydantic import BaseModel, Field, ConfigDict, StringConstraints, field_validator
from typing import Optional, Any, Annotated

BarangayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class HazardCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1)
    hazard_type: str  # 'latrine', 'septic_tank', 'piggery', 'river', 'stream', 'drainage', 'agricultural_land', 'farmland', 'other'
    geometry_type: str  # 'Point', 'LineString', 'Polygon'
    coordinates: Any  # [lng, lat] for Point, [[lng, lat], ...] for LineString, [[[lng, lat], ...]] for Polygon
    barangay: BarangayName
    risk_level: Optional[str] = "high"  # 'high', 'medium', 'low'
    notes: Optional[str] = None


class HazardUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    hazard_type: Optional[str] = None
    barangay: Optional[BarangayName] = None
    risk_level: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("barangay")
    @classmethod
    def require_designation_if_supplied(cls, value):
        if value is None:
            raise ValueError("A designated barangay cannot be cleared")
        return value
