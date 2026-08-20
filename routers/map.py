import httpx
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse

from core.database import get_supabase
from core.dependencies import get_current_user

router = APIRouter(prefix="/api/map", tags=["Map"])


@router.get("/weather")
async def get_weather(
    current_user: Annotated[dict, Depends(get_current_user)],
    lat: float = Query(10.1333),
    lon: float = Query(124.8333)
):
    """Get current weather from OpenWeather API for Maasin."""
    # In a real scenario, this would use an API key from settings.
    # For now, we return mock data based on the old app's logic.
    mock_weather = {
        "weather": [{"description": "scattered clouds", "icon": "03d"}],
        "main": {"temp": 28.5, "humidity": 75},
        "wind": {"speed": 3.2},
        "rain": {"1h": 0.5} # mm
    }
    return {"success": True, "data": mock_weather}
