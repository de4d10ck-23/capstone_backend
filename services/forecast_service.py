import os
import sys
import json
import logging
import joblib
import numpy as np
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
MODEL_PATH = os.path.join(BACKEND_ROOT, "ml", "water_quality_model.pkl")
METRICS_PATH = os.path.join(BACKEND_ROOT, "ml", "model_evaluation_metrics.json")

from core.database import get_supabase
from core.spatial import calculate_station_hazard_distances
from services.weather_service import get_live_weather
from ml.dataset import encode_source_type, count_buffer_encroachments, FEATURE_COLUMNS

_cached_model_payload: Optional[Dict[str, Any]] = None


def load_model_payload(force_reload: bool = False) -> Dict[str, Any]:
    """Load or return cached model pipeline and metadata."""
    global _cached_model_payload
    if _cached_model_payload is None or force_reload:
        if not os.path.exists(MODEL_PATH):
            logger.warning(f"Model file not found at {MODEL_PATH}. Retraining may be needed.")
            return {}
        try:
            _cached_model_payload = joblib.load(MODEL_PATH)
            logger.info("Successfully loaded Random Forest model into memory.")
        except Exception as e:
            logger.error(f"Error loading model from {MODEL_PATH}: {e}")
            return {}
    return _cached_model_payload


def get_model_status() -> Dict[str, Any]:
    """Retrieve full model metadata, evaluation metrics, and feature importance."""
    payload = load_model_payload()
    metrics = {}
    if os.path.exists(METRICS_PATH):
        try:
            with open(METRICS_PATH, "r") as f:
                metrics = json.load(f)
        except Exception as e:
            logger.error(f"Error reading metrics JSON: {e}")

    return {
        "model_loaded": bool(payload and "pipeline" in payload),
        "model_type": payload.get("model_type", "RandomForestClassifier"),
        "trained_at": payload.get("trained_at"),
        "features": payload.get("feature_columns", FEATURE_COLUMNS),
        "feature_importance": payload.get("feature_importance", []),
        "test_metrics": payload.get("test_metrics", {}),
        "cross_validation_5fold": metrics.get("cross_validation_5fold", {}),
        "held_out_test_metrics": metrics.get("held_out_test_metrics", {}),
        "total_samples": metrics.get("total_samples", 0),
    }


def get_active_config() -> Dict[str, Any]:
    """Fetch active threshold config from database or return standard defaults."""
    sb = get_supabase()
    res = sb.table("forecast_config").select("*").limit(1).execute()
    if res.data and len(res.data) > 0:
        cfg = res.data[0]
        return {
            "risk_threshold_low": float(cfg.get("risk_threshold_low") or 0.30),
            "risk_threshold_high": float(cfg.get("risk_threshold_high") or 0.65),
            "prediction_days": int(cfg.get("prediction_days") or 7),
            "rainfall_weight": float(cfg.get("rainfall_weight") or 0.30),
            "temperature_weight": float(cfg.get("temperature_weight") or 0.20),
            "humidity_weight": float(cfg.get("humidity_weight") or 0.15),
            "proximity_weight": float(cfg.get("proximity_weight") or 0.20),
            "historical_weight": float(cfg.get("historical_weight") or 0.15),
        }
    return {
        "risk_threshold_low": 0.30,
        "risk_threshold_high": 0.65,
        "prediction_days": 7,
        "rainfall_weight": 0.30,
        "temperature_weight": 0.20,
        "humidity_weight": 0.15,
        "proximity_weight": 0.20,
        "historical_weight": 0.15,
    }


async def predict_station_risk(
    station_id: str,
    custom_threshold_low: Optional[float] = None,
    custom_threshold_high: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Perform real-time machine learning inference for a given water station:
    1. Retrieve station coordinates and source type
    2. Retrieve GIS mapped hazards and calculate proximity distances
    3. Retrieve live local meteorological conditions
    4. Compute model.predict_proba()
    5. Evaluate against configurable thresholds
    """
    sb = get_supabase()
    loc_res = sb.table("water_locations").select("*").eq("id", station_id).execute()
    if not loc_res.data:
        raise ValueError(f"Water station with ID {station_id} not found.")

    station = loc_res.data[0]
    lat = float(station.get("latitude") or 10.1330)
    lng = float(station.get("longitude") or 124.8700)
    source_type = station.get("source_type") or ""
    station_name = station.get("full_name") or station.get("name") or ""
    barangay = station.get("barangay") or ""

    # Fetch mapped hazards
    hazards_res = sb.table("contamination_hazards").select("*").execute()
    hazards = hazards_res.data or []

    # Proximity metrics
    coord = (lng, lat)
    dist_dict = calculate_station_hazard_distances(coord, hazards)
    encroachments = count_buffer_encroachments(coord, hazards, 50.0)

    # Weather metrics
    weather_data = await get_live_weather(lat, lng)
    rf_24h = float(weather_data.get("rainfall_24h_mm") or 0.0)
    rf_48h = float(weather_data.get("rainfall_48h_mm") or (rf_24h * 1.5))
    temp = float(weather_data.get("temperature_celsius") or 28.0)
    humidity = float(weather_data.get("humidity_percent") or 75.0)

    # Encode source type
    source_code = encode_source_type(source_type, station_name)

    # Prepare feature vector matching FEATURE_COLUMNS
    feature_vector = [
        dist_dict["min_dist_latrine_meters"],
        dist_dict["min_dist_river_meters"],
        dist_dict["min_dist_farmland_meters"],
        encroachments,
        rf_24h,
        rf_48h,
        temp,
        humidity,
        source_code,
    ]

    # Load model
    payload = load_model_payload()
    if not payload or "pipeline" not in payload:
        raise RuntimeError("ML model is not loaded. Please train or load the model.")

    pipeline = payload["pipeline"]
    proba = pipeline.predict_proba([feature_vector])[0]
    # proba[1] is probability of unsafe/contaminated
    risk_score = round(float(proba[1]), 4)

    # Load thresholds
    cfg = get_active_config()
    threshold_low = custom_threshold_low if custom_threshold_low is not None else cfg["risk_threshold_low"]
    threshold_high = custom_threshold_high if custom_threshold_high is not None else cfg["risk_threshold_high"]

    # Classify status and warning level
    if risk_score < threshold_low:
        predicted_status = "safe"
        risk_level = "Low"
        advisory = "Water parameters indicate clean, potable condition with minimal microbial runoff risk."
    elif risk_score <= threshold_high:
        predicted_status = "warning"
        risk_level = "Medium (At Risk)"
        advisory = "Caution advised: Environmental indicators suggest moderate risk of surface runoff infiltration."
    else:
        predicted_status = "undrinkable"
        risk_level = "High (Contaminated)"
        advisory = "High Contamination Risk: Elevated probability of E. coli infiltration. Precautionary boiling strongly advised."

    # Identify primary risk contributors
    risk_factors = []
    if dist_dict["min_dist_latrine_meters"] < 50:
        risk_factors.append(f"Unhygienic latrine within {dist_dict['min_dist_latrine_meters']}m buffer zone")
    if rf_24h >= 20.0 or rf_48h >= 40.0:
        risk_factors.append(f"Heavy rainfall ({rf_24h} mm in 24h) inducing surface runoff")
    if dist_dict["min_dist_river_meters"] < 60:
        risk_factors.append(f"Proximity to river/drainage canal ({dist_dict['min_dist_river_meters']}m)")
    if dist_dict["min_dist_farmland_meters"] < 100:
        risk_factors.append(f"Adjacent agricultural runoff zone ({dist_dict['min_dist_farmland_meters']}m)")
    if encroachments > 0:
        risk_factors.append(f"{encroachments} hazardous facilities encroaching within 50m")

    return {
        "station_id": station_id,
        "station_name": station_name,
        "barangay": barangay,
        "coordinates": {"latitude": lat, "longitude": lng},
        "risk_probability": risk_score,
        "risk_percentage": round(risk_score * 100, 1),
        "predicted_status": predicted_status,
        "risk_level": risk_level,
        "advisory": advisory,
        "thresholds_used": {
            "low_cutoff": threshold_low,
            "high_cutoff": threshold_high,
        },
        "environmental_metrics": {
            "distance_to_latrine_meters": dist_dict["min_dist_latrine_meters"],
            "distance_to_river_meters": dist_dict["min_dist_river_meters"],
            "distance_to_farmland_meters": dist_dict["min_dist_farmland_meters"],
            "buffer_encroachments_count": encroachments,
            "rainfall_24h_mm": rf_24h,
            "rainfall_48h_mm": rf_48h,
            "temperature_celsius": temp,
            "humidity_percent": humidity,
            "source_type_code": source_code,
        },
        "risk_factors": risk_factors,
        "weather_source": weather_data.get("source", "OpenWeather"),
        "evaluated_at": datetime.now().isoformat(),
    }


async def predict_all_stations(
    custom_threshold_low: Optional[float] = None,
    custom_threshold_high: Optional[float] = None,
) -> Dict[str, Any]:
    """Run batch prediction on all water locations."""
    sb = get_supabase()
    locs_res = sb.table("water_locations").select("id, full_name, barangay").execute()
    stations = locs_res.data or []

    results = []
    summary = {"safe": 0, "warning": 0, "undrinkable": 0, "total": len(stations)}

    for s in stations:
        try:
            pred = await predict_station_risk(
                s["id"], custom_threshold_low, custom_threshold_high
            )
            results.append(pred)
            status = pred["predicted_status"]
            if status in summary:
                summary[status] += 1
        except Exception as e:
            logger.error(f"Error evaluating station {s['id']}: {e}")

    return {
        "summary": summary,
        "results": results,
        "total_evaluated": len(results),
        "timestamp": datetime.now().isoformat(),
    }
