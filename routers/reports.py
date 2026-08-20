from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from core.database import get_supabase
from core.dependencies import get_current_user, require_admin_or_inspector, require_admin_cho_inspector
from models.report import ReportGenerate

router = APIRouter(prefix="/api/reports", tags=["Reports"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_reports(
    current_user: Annotated[dict, Depends(require_staff)]
):
    """List generated reports."""
    sb = get_supabase()
    result = sb.table("reports").select("*").order("created_at", desc=True).execute()
    return {"success": True, "data": result.data or []}


@router.post("/generate")
async def generate_report(
    body: ReportGenerate,
    current_user: Annotated[dict, Depends(require_admin_or_inspector)]
):
    """Generate a new report. Admin and Inspector only."""
    sb = get_supabase()

    # In a real app, this would trigger a background task to generate a PDF/CSV.
    # For now, we just create a record in the database.
    new_report = {
        "title": body.title.strip(),
        "type": body.type,
        "barangay": body.barangay,
        "period_start": body.period_start,
        "period_end": body.period_end,
        "generated_by": current_user["id"],
        "status": "completed", # Simulate immediate completion
        "file_url": None, # Would point to Supabase Storage in reality
    }

    result = sb.table("reports").insert(new_report).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to generate report")

    return {"success": True, "message": "Report generated successfully", "data": result.data[0]}


@router.get("/{report_id}/download")
async def download_report(
    report_id: str,
    current_user: Annotated[dict, Depends(require_admin_cho_inspector)]
):
    """Download a report file. Returns mock CSV content for now."""
    sb = get_supabase()
    result = sb.table("reports").select("*").eq("id", report_id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Report not found")

    report = result.data
    
    # Mock CSV content
    csv_content = f"Report Title,{report['title']}\nType,{report['type']}\nGenerated At,{report['created_at']}\n"
    csv_content += "\nID,Full Name,Status\n1,Sample Source,Safe\n"

    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=report_{report_id}.csv"}
    )
