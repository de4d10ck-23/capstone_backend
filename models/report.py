from pydantic import BaseModel, Field
from typing import Optional
from datetime import date


class ReportGenerate(BaseModel):
    title: str = Field("", max_length=200)
    type: Optional[str] = "barangay_endorsement"
    report_type: Optional[str] = None
    barangay: Optional[str] = None
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    include_forecasts: bool = False
    notes: str = Field("", max_length=5000)
    description: Optional[str] = None
    severity: Optional[str] = None


class ReportReviewComplete(BaseModel):
    notes: str = Field(..., min_length=1, max_length=5000)

