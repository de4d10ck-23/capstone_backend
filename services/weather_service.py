import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional
import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# Default coordinates for Maasin City, Southern Leyte
MAASIN_DEFAULT_LAT = 10.1330
MAASIN_DEFAULT_LNG = 124.8700

# Simple in-memory cache for live weather to avoid API rate limits:
# Key: (round(lat, 2), round(lng, 2)), Value: (timestamp, weather_data)
_LIVE_WEATHER_CACHE: Dict[tuple, tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 600  # 10 minutes cache


async def get_historical_weather(
    target_date: str,
    lat: float = MAASIN_DEFAULT_LAT,
    lng: float = MAASIN_DEFAULT_LNG,
) -> Dict[str, Any]:
    """
    Fetch historical meteorological data for a specific date in Maasin City.
    Uses free Open-Meteo Historical Archive API (no API key required).
    
    Extracts:
      - rainfall_intensity_mm: 24h precipitation on the target date (mm)
      - cumulative_48h_rainfall_mm: 48h precipitation leading up to and including the date (mm)
      - rainfall_frequency_days: Count of days with measurable rain (>0.1mm) in 48h window
      - temperature_celsius: Mean daily ambient temperature (°C)
      - humidity_percent: Mean relative humidity (%)
      - precipitation_hours: Number of hours with rainfall
    """
    try:
        # Parse date and compute start_date as 1 day before to capture 48h runoff window
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        start_dt = target_dt - timedelta(days=1)
        start_date_str = start_dt.strftime("%Y-%m-%d")

        url = (
            f"https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lng}"
            f"&start_date={start_date_str}&end_date={target_date}"
            f"&daily=precipitation_sum,temperature_2m_mean,relative_humidity_2m_mean,precipitation_hours"
            f"&timezone=Asia%2FManila"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily", {})
        precip_list = daily.get("precipitation_sum", [0.0])
        temp_list = daily.get("temperature_2m_mean", [26.5])
        humidity_list = daily.get("relative_humidity_2m_mean", [80.0])
        hours_list = daily.get("precipitation_hours", [0.0])

        # Target day is the last index in the response list
        day_precip = float(precip_list[-1]) if precip_list and precip_list[-1] is not None else 0.0
        cum_48h = sum(float(p) for p in precip_list if p is not None)
        mean_temp = float(temp_list[-1]) if temp_list and temp_list[-1] is not None else 26.5
        mean_humidity = float(humidity_list[-1]) if humidity_list and humidity_list[-1] is not None else 80.0
        precip_hours = float(hours_list[-1]) if hours_list and hours_list[-1] is not None else 0.0

        # Frequency: days with rain > 0.1 mm in the window
        rain_days = sum(1 for p in precip_list if p is not None and float(p) > 0.1)

        return {
            "date": target_date,
            "latitude": lat,
            "longitude": lng,
            "rainfall_intensity_mm": round(day_precip, 2),
            "cumulative_48h_rainfall_mm": round(cum_48h, 2),
            "rainfall_frequency_days": rain_days,
            "temperature_celsius": round(mean_temp, 1),
            "humidity_percent": round(mean_humidity, 1),
            "precipitation_hours": round(precip_hours, 1),
            "source": "open-meteo-archive",
        }

    except Exception as exc:
        logger.warning(f"Could not retrieve historical weather for {target_date}: {exc}")
        # Safe climatological baseline defaults for Maasin City (tropical maritime climate)
        return {
            "date": target_date,
            "latitude": lat,
            "longitude": lng,
            "rainfall_intensity_mm": 2.5,
            "cumulative_48h_rainfall_mm": 5.0,
            "rainfall_frequency_days": 1,
            "temperature_celsius": 27.0,
            "humidity_percent": 82.0,
            "precipitation_hours": 2.0,
            "source": "fallback-baseline",
        }


async def get_live_weather(
    lat: float = MAASIN_DEFAULT_LAT,
    lng: float = MAASIN_DEFAULT_LNG,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Fetch real-time weather in Maasin City for current ML risk inference.
    1. Checks in-memory cache (10-minute TTL).
    2. Calls OpenWeather API using configured OPENWEATHER_API_KEY.
    3. Seamlessly falls back to Open-Meteo Current Weather if OpenWeather fails.
    """
    cache_key = (round(lat, 2), round(lng, 2))
    now = time.time()

    if not force_refresh and cache_key in _LIVE_WEATHER_CACHE:
        cached_time, cached_data = _LIVE_WEATHER_CACHE[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            return {**cached_data, "cached": True}

    # Attempt 1: OpenWeather API
    api_key = settings.OPENWEATHER_API_KEY
    if api_key:
        try:
            owm_url = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?lat={lat}&lon={lng}&appid={api_key}&units=metric"
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(owm_url)
                if res.status_code == 200:
                    owm = res.json()
                    main = owm.get("main", {})
                    weather_arr = owm.get("weather", [{}])
                    rain = owm.get("rain", {})
                    # Rain in the last 1h (or fallback 0.0)
                    rain_1h = float(rain.get("1h", 0.0))

                    result = {
                        "latitude": lat,
                        "longitude": lng,
                        "rainfall_intensity_mm": round(rain_1h, 2),
                        "rainfall_frequency_days": 1.0 if rain_1h > 0.1 else 0.0,
                        "temperature_celsius": round(float(main.get("temp", 27.0)), 1),
                        "humidity_percent": round(float(main.get("humidity", 80.0)), 1),
                        "weather_description": weather_arr[0].get("description", "Normal").title(),
                        "wind_speed_mps": round(float(owm.get("wind", {}).get("speed", 0.0)), 1),
                        "source": "openweather",
                        "cached": False,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    _LIVE_WEATHER_CACHE[cache_key] = (now, result)
                    return result
                else:
                    logger.warning(f"OpenWeather returned HTTP {res.status_code}: {res.text}")
        except Exception as owm_exc:
            logger.warning(f"OpenWeather API request failed: {owm_exc}")

    # Attempt 2: Fallback to Open-Meteo Current Weather
    try:
        om_url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lng}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m"
            f"&daily=precipitation_sum"
            f"&timezone=Asia%2FManila"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(om_url)
            res.raise_for_status()
            om = res.json()
            curr = om.get("current", {})
            daily = om.get("daily", {})
            daily_precip = daily.get("precipitation_sum", [0.0])[0] or 0.0

            result = {
                "latitude": lat,
                "longitude": lng,
                "rainfall_intensity_mm": round(float(curr.get("precipitation", daily_precip)), 2),
                "rainfall_frequency_days": 1.0 if daily_precip > 0.1 else 0.0,
                "temperature_celsius": round(float(curr.get("temperature_2m", 27.0)), 1),
                "humidity_percent": round(float(curr.get("relative_humidity_2m", 80.0)), 1),
                "weather_description": "Normal (Open-Meteo)",
                "wind_speed_mps": round(float(curr.get("wind_speed_10m", 0.0)), 1),
                "source": "open-meteo-live",
                "cached": False,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _LIVE_WEATHER_CACHE[cache_key] = (now, result)
            return result
    except Exception as om_exc:
        logger.warning(f"Open-Meteo fallback failed: {om_exc}")

    # Ultimate fallback
    fallback_res = {
        "latitude": lat,
        "longitude": lng,
        "rainfall_intensity_mm": 0.0,
        "rainfall_frequency_days": 0.0,
        "temperature_celsius": 27.0,
        "humidity_percent": 80.0,
        "weather_description": "Clear (Baseline)",
        "wind_speed_mps": 2.0,
        "source": "fallback-baseline",
        "cached": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return fallback_res
