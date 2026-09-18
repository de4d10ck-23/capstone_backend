from typing import Annotated, Optional
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks

from core.database import get_supabase
from core.dependencies import require_admin, require_staff, get_optional_user
from models.forecast import ForecastConfig
from services.forecast_service import (
    get_model_status,
    predict_station_risk,
    predict_all_stations,
    load_model_payload,
)
from services.heatmap_service import (
    get_heatmaps_data,
    trigger_manual_heatmap_refresh,
)
from ml.train_model import run_training_tournament

router = APIRouter(prefix="/api/forecast", tags=["Forecast"])


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
        return {"success": True, "data": batch_results}
    except Exception as e:
        logger.error(f"Batch prediction error: {e}")
        raise HTTPException(status_code=500, detail=f"Batch evaluation error: {str(e)}")


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

