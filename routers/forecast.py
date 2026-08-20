from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_supabase
from core.dependencies import require_admin
from models.forecast import ForecastConfig

router = APIRouter(prefix="/api/forecast", tags=["Forecast"])


@router.get("/config")
async def get_forecast_config(
    current_user: Annotated[dict, Depends(require_admin)]
):
    """Get the current ML forecast configuration. Admin only."""
    sb = get_supabase()
    result = sb.table("forecast_config").select("*").limit(1).execute()
    
    if not result.data:
        # Return defaults if not configured yet
        return {"success": True, "data": ForecastConfig().model_dump()}
        
    return {"success": True, "data": result.data[0]}


@router.put("/config")
async def update_forecast_config(
    body: ForecastConfig,
    current_user: Annotated[dict, Depends(require_admin)]
):
    """Update ML forecast parameters. Admin only."""
    sb = get_supabase()
    
    update_data = {k: v for k, v in body.model_dump().items() if v is not None}
    
    # Check if a row exists
    existing = sb.table("forecast_config").select("id").limit(1).execute()
    
    if existing.data:
        result = sb.table("forecast_config").update(update_data).eq("id", existing.data[0]["id"]).execute()
    else:
        result = sb.table("forecast_config").insert(update_data).execute()
        
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save forecast config")
        
    return {"success": True, "message": "Configuration saved", "data": result.data[0]}
