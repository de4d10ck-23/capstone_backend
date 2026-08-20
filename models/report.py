from pydantic import BaseModel, Field
from typing import Optional


class ReportGenerate(BaseModel):
    title: str = Field(..., min_length=1)
    type: str  # water_quality | risk_assessment | barangay_summary
    barangay: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
