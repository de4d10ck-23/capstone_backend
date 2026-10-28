from typing import Annotated, Optional
import logging
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks

from core.database import get_supabase
from core.dependencies import require_admin, require_staff, get_optional_user
from models.forecast import ForecastConfig
from services.saved_forecast_service import get_saved_forecasts, refresh_saved_forecasts
from services.forecast_service import (
    get_model_status,
    predict_station_risk,
    predict_all_stations,
    load_model_payload,
    run_barangay_surveillance_and_alert,
)
from pydantic import BaseModel, Field
from services.heatmap_service import (
    get_heatmaps_data,
    trigger_manual_heatmap_refresh,
    get_scheduler_status,
    set_run_interval_minutes,
    MIN_INTERVAL_MINUTES,
    MAX_INTERVAL_MINUTES,
)
from ml.train_model import run_training_tournament

router = APIRouter(prefix="/api/forecast", tags=["Forecast"])
logger = logging.getLogger(__name__)


@router.get("/map")
async def get_map_forecast(
    forecast_date: date = Query(...),
    barangay: Optional[str] = Query(None),
):
    """Read-only date-specific risk estimates; does not update records or send alerts."""
    today = datetime.now(timezone(timedelta(hours=8))).date()
    if not 0 <= (forecast_date - today).days <= 7:
        raise HTTPException(422, "Choose today through seven days ahead (Asia/Manila).")
    try:
        return {"success": True, "data": get_saved_forecasts(forecast_date.isoformat(), barangay)}
    except Exception:
        logger.exception("Could not read saved forecasts")
        raise HTTPException(503, "Saved forecasts are unavailable. Ask an administrator to check the forecast storage setup.")



@router.get("/config")
async def get_forecast_config(
    current_user: Annotated[dict, Depends(require_admin)]
):
    """Get the current ML forecast configuration and probability thresholds. Admin only."""
    sb = get_supabase()
    result = sb.table("forecast_config").select("*").limit(1).execute()

    if not result.data:
        return {"success": True, "data": ForecastConfig().model_dump()}

    return {"success": True, "data": result.data[0]}


@router.put("/config")
async def update_forecast_config(
    body: ForecastConfig,
    current_user: Annotated[dict, Depends(require_admin)]
):
    """Update ML forecast parameters, weights, and risk thresholds. Admin only."""
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

    return {"success": True, "message": "Configuration saved successfully", "data": result.data[0]}


@router.get("/model-status")
async def get_ml_model_status(
    current_user: Annotated[dict, Depends(require_admin)]
):
    """
    Retrieve machine learning model metadata, training timestamp, 
    5-Fold Cross Validation results, held-out test evaluation metrics, 
    and feature importance rankings.
    """
    try:
        status_data = get_model_status()
        return {"success": True, "data": status_data}
    except Exception as e:
        logger.error(f"Error retrieving model status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/retrain")
async def retrain_model_endpoint(
    current_user: Annotated[dict, Depends(require_admin)]
):
    """
    Trigger machine learning retraining across latest laboratory testing data 
    and participatory GIS hazards.
    """
    try:
        logger.info("Admin triggered ML model retraining...")
        report = run_training_tournament()
        # Force reload cached model in memory
        load_model_payload(force_reload=True)
        return {
            "success": True,
            "message": "Random Forest model successfully retrained on latest laboratory & spatial data.",
            "metrics": report,
        }
    except Exception as e:
        logger.error(f"Retraining failed: {e}")
        raise HTTPException(status_code=500, detail=f"Model retraining failed: {str(e)}")


@router.get("/predict/{location_id}")
async def predict_single_location(
    location_id: str,
    threshold_low: Optional[float] = Query(None, description="Optional override for low risk threshold"),
    threshold_high: Optional[float] = Query(None, description="Optional override for high risk threshold"),
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """
    Generate live risk forecast for a specific water station combining 
    participatory GIS proximity, real-time weather, and Random Forest probability inference.
    """
    try:
        result = await predict_station_risk(
            station_id=location_id,
            custom_threshold_low=threshold_low,
            custom_threshold_high=threshold_high,
        )
        return {"success": True, "data": result}
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except Exception as e:
        logger.error(f"Prediction error for station {location_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Inference error: {str(e)}")


@router.post("/run-batch")
async def run_batch_predictions(
    current_user: Annotated[dict, Depends(require_staff)],
    threshold_low: Optional[float] = None,
    threshold_high: Optional[float] = None,
):
    """
    Execute batch prediction across all active water stations in Maasin City.
    """
    try:
        batch_results = await predict_all_stations(
            custom_threshold_low=threshold_low,
            custom_threshold_high=threshold_high,
        )
        batch_results["forecast_refresh"] = await refresh_saved_forecasts()
        return {"success": True, "data": batch_results}
    except Exception as e:
        logger.error(f"Batch prediction error: {e}")
        raise HTTPException(status_code=500, detail=f"Batch evaluation error: {str(e)}")


@router.post("/run-surveillance")
async def run_surveillance_endpoint(
    current_user: Annotated[dict, Depends(require_admin)],
    barangay: Optional[str] = Query(None, description="Target barangay or 'all'"),
    notify_residents: bool = Query(True, description="Whether to send push notification to residents if risk is flagged"),
    threshold_low: Optional[float] = Query(None, description="Optional override for low risk threshold"),
    threshold_high: Optional[float] = Query(None, description="Optional override for high risk threshold"),
    bypass_daily_limit: bool = Query(False, description="Ignore the 1-push-per-station-per-day limit (testing only)"),
):
    """
    On-demand manual trigger to execute machine learning risk surveillance 
    across all water stations in a selected barangay (or all barangays).
    Only CONTAMINATED stations dispatch in-app notifications and Web Push alerts
    to residents, max once per water source per day (unless bypass_daily_limit).
    """
    try:
        refresh = await refresh_saved_forecasts()
        results = await run_barangay_surveillance_and_alert(
            barangay=barangay,
            notify_residents=notify_residents,
            current_user_id=current_user.get("id"),
            triggered_by="admin_on_demand",
            bypass_daily_limit=bypass_daily_limit,
        )
        results["forecast_refresh"] = refresh
        return {"success": True, "data": results}
    except Exception as e:
        logger.error(f"Error in on-demand surveillance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Surveillance run failed: {str(e)}")


@router.get("/heatmaps")
async def get_heatmaps_endpoint(
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """
    Retrieve precomputed GeoJSON heatmaps for:
    1. Hazard point, line, and polygon spatial influence
    2. Water source contamination and microbial risk
    Includes last_updated timestamp and next_run schedule.
    """
    try:
        data = await get_heatmaps_data()
        return {"success": True, "data": data}
    except Exception as e:
        logger.error(f"Error fetching heatmaps: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate heatmaps: {str(e)}")


@router.post("/heatmaps/refresh")
async def refresh_heatmaps_endpoint(
    current_user: Annotated[dict, Depends(require_admin)],
):
    """
    Admin manual trigger: Bypasses the 1-hour schedule and immediately recalculates
    both the hazard heatmap and water contamination heatmap with fresh inferences.
    """
    try:
        logger.info(f"Admin {current_user.get('email', 'unknown')} manually triggered heatmap recalculation.")
        refreshed = await trigger_manual_heatmap_refresh()
        return {
            "success": True,
            "message": "Both Hazard and Water Contamination heatmaps successfully recomputed.",
            "data": refreshed,
        }
    except Exception as e:
        logger.error(f"Manual heatmap refresh error: {e}")
        raise HTTPException(status_code=500, detail=f"Recalculation error: {str(e)}")


class ScheduleUpdate(BaseModel):
    run_interval_minutes: int = Field(..., ge=MIN_INTERVAL_MINUTES, le=MAX_INTERVAL_MINUTES)


@router.get("/schedule")
async def get_schedule_endpoint(
    current_user: Annotated[dict, Depends(require_admin)],
):
    """Get the background model re-run interval (minutes) and next scheduled run."""
    return {"success": True, "data": get_scheduler_status()}


@router.put("/schedule")
async def update_schedule_endpoint(
    body: ScheduleUpdate,
    current_user: Annotated[dict, Depends(require_admin)],
):
    """Update how many minutes the background engine waits before re-running the model."""
    try:
        set_run_interval_minutes(body.run_interval_minutes)
        return {
            "success": True,
            "message": f"Model will now re-run every {body.run_interval_minutes} minute(s).",
            "data": get_scheduler_status(),
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Failed to update schedule interval: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update interval: {str(e)}")

