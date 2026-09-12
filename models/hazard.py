from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Any


class HazardCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1)
    hazard_type: str  # 'latrine', 'septic_tank', 'piggery', 'river', 'stream', 'drainage', 'agricultural_land', 'farmland', 'other'
    geometry_type: str  # 'Point', 'LineString', 'Polygon'
    coordinates: Any  # [lng, lat] for Point, [[lng, lat], ...] for LineString, [[[lng, lat], ...]] for Polygon
    barangay: Optional[str] = None
    risk_level: Optional[str] = "high"  # 'high', 'medium', 'low'
    notes: Optional[str] = None


class HazardUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None
    hazard_type: Optional[str] = None
    risk_level: Optional[str] = None
    notes: Optional[str] = None
