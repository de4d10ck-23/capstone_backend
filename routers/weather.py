from typing import Annotated, Optional
from fastapi import APIRouter, Query, Depends

from core.dependencies import get_optional_user
from services.weather_service import get_live_weather, get_historical_weather, MAASIN_DEFAULT_LAT, MAASIN_DEFAULT_LNG

router = APIRouter(prefix="/api/weather", tags=["Weather"])


@router.get("/current")
async def get_current_weather(
    lat: float = Query(default=MAASIN_DEFAULT_LAT, ge=-90.0, le=90.0),
    lng: float = Query(default=MAASIN_DEFAULT_LNG, ge=-180.0, le=180.0),
    refresh: bool = Query(default=False),
    _user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """
    Retrieve real-time meteorological conditions for Maasin City (or given coordinates).
    Uses OpenWeather API with Open-Meteo fallback.
    """
    data = await get_live_weather(lat=lat, lng=lng, force_refresh=refresh)
    return {"success": True, "data": data}


@router.get("/historical")
async def get_historical_weather_endpoint(
    date: str = Query(..., regex=r"^\d{4}-\d{2}-\d{2}$", description="Target date in YYYY-MM-DD format"),
    lat: float = Query(default=MAASIN_DEFAULT_LAT, ge=-90.0, le=90.0),
    lng: float = Query(default=MAASIN_DEFAULT_LNG, ge=-180.0, le=180.0),
    _user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """
    Retrieve historical meteorological records for a specific sampling date in Maasin City.
    Uses Open-Meteo Historical Archive API.
    """
    data = await get_historical_weather(target_date=date, lat=lat, lng=lng)
    return {"success": True, "data": data}
