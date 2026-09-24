from pydantic import BaseModel, Field
from typing import Optional


class ReportGenerate(BaseModel):
    title: str = Field(..., min_length=1)
    type: Optional[str] = "barangay_endorsement"
    report_type: Optional[str] = None
    barangay: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    description: Optional[str] = None
    severity: Optional[str] = None

