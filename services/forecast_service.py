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
from services.weather_service import get_live_weather
from services.push_service import broadcast_push_notification
from ml.dataset import encode_source_type, get_station_hazard_levels, FEATURE_COLUMNS

_cached_model_payload: Optional[Dict[str, Any]] = None

# ---------------------------------------------------------------------------
# Per-station daily push limit (anti-spam)
# Each contaminated water source may trigger at most ONE resident push per day.
# ---------------------------------------------------------------------------
PUSH_ALERT_LOG_PATH = os.path.join(BACKEND_ROOT, "data", "push_alert_log.json")


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _load_push_alert_log() -> Dict[str, str]:
    """Return {station_id: 'YYYY-MM-DD' of last push}, pruned to today's entries."""
    if not os.path.exists(PUSH_ALERT_LOG_PATH):
        return {}
    try:
        with open(PUSH_ALERT_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        today = _today_str()
        return {k: v for k, v in data.items() if v == today}
    except Exception as e:
        logger.warning(f"Could not read push alert log: {e}")
        return {}


def _save_push_alert_log(log: Dict[str, str]) -> None:
    try:
        os.makedirs(os.path.dirname(PUSH_ALERT_LOG_PATH), exist_ok=True)
        with open(PUSH_ALERT_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save push alert log: {e}")


def was_station_alerted_today(station_id: str) -> bool:
    return _load_push_alert_log().get(str(station_id)) == _today_str()


def mark_stations_alerted_today(station_ids: List[str]) -> None:
    log = _load_push_alert_log()
    today = _today_str()
    for sid in station_ids:
        log[str(sid)] = today
    _save_push_alert_log(log)


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
    2. Retrieve official GIS flood and landslide susceptibility levels
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

    # Official GIS hazard susceptibility
    flood_level, landslide_level = get_station_hazard_levels(lat, lng)

    # Weather metrics
    weather_data = await get_live_weather(lat, lng)
    rf_24h = float(weather_data.get("rainfall_24h_mm") or 0.0)
    rf_48h = float(weather_data.get("rainfall_48h_mm") or (rf_24h * 1.5))
    temp = float(weather_data.get("temperature_celsius") or 28.0)
    humidity = float(weather_data.get("humidity_percent") or 75.0)

    # Encode source protection tier
    source_code = encode_source_type(source_type, station_name)

    # Prepare feature vector matching FEATURE_COLUMNS
    feature_vector = [
        flood_level,
        landslide_level,
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
        advisory = "High Contamination Risk: Elevated probability of microbial infiltration. Precautionary boiling strongly advised."

    # Identify primary risk contributors based on official parameters
    risk_factors = []
    if flood_level == 2:
        risk_factors.append("Station is located inside High Flood Susceptibility zone (DENR-MGB)")
    elif flood_level == 1:
        risk_factors.append("Station is located inside Moderate Flood Susceptibility zone (DENR-MGB)")

    if landslide_level == 2:
        risk_factors.append("Station is located inside High Landslide Susceptibility zone (DENR-MGB)")
    elif landslide_level == 1:
        risk_factors.append("Station is located inside Moderate Landslide Susceptibility zone (DENR-MGB)")

    if rf_24h >= 15.0 or rf_48h >= 30.0:
        risk_factors.append(f"Elevated rainfall ({rf_24h} mm in 24h, {rf_48h} mm in 48h) inducing surface runoff")

    if source_code == 0:
        risk_factors.append("Open spring water source with vulnerable surface intake")
    elif source_code == 2 and flood_level >= 1:
        risk_factors.append("Groundwater well head susceptible to flood backwash")

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
            "flood_susceptibility_level": flood_level,
            "flood_susceptibility_label": "High" if flood_level == 2 else ("Moderate" if flood_level == 1 else "Low/None"),
            "landslide_susceptibility_level": landslide_level,
            "landslide_susceptibility_label": "High" if landslide_level == 2 else ("Moderate" if landslide_level == 1 else "Low/None"),
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


async def run_barangay_surveillance_and_alert(
    barangay: Optional[str] = None,
    notify_residents: bool = True,
    custom_threshold_low: Optional[float] = None,
    custom_threshold_high: Optional[float] = None,
    current_user_id: Optional[str] = None,
    triggered_by: str = "admin_manual",
    bypass_daily_limit: bool = False,
) -> Dict[str, Any]:
    """
    Run machine learning risk surveillance across water stations in a selected barangay (or all barangays).

    Resident alert policy (anti-spam):
      * Only CONTAMINATED (undrinkable) stations trigger in-app + Web Push alerts.
        Warning / at-risk stations are reported in the results but residents are NOT notified.
      * Each water source triggers at most ONE push per calendar day. If the same station is
        flagged contaminated again on a later run the same day, it is silently suppressed.
    """
    sb = get_supabase()
    query = sb.table("water_locations").select("id, full_name, barangay, latitude, longitude, status")

    target_b = (barangay or "").strip()
    if target_b and target_b.lower() not in ("all", "all barangays", "city-wide", ""):
        query = query.ilike("barangay", target_b)

    locs_res = query.execute()
    stations = locs_res.data or []

    results = []
    summary = {
        "total": len(stations),
        "safe": 0,
        "warning": 0,
        "undrinkable": 0,
    }
    flagged_by_barangay: Dict[str, List[Dict[str, Any]]] = {}

    for s in stations:
        try:
            pred = await predict_station_risk(
                s["id"], custom_threshold_low, custom_threshold_high
            )
            results.append(pred)
            status = pred.get("predicted_status", "safe")
            if status in summary:
                summary[status] += 1

            if status in ("warning", "undrinkable"):
                b_name = pred.get("barangay") or s.get("barangay") or "Maasin City"
                if b_name not in flagged_by_barangay:
                    flagged_by_barangay[b_name] = []
                flagged_by_barangay[b_name].append(pred)
        except Exception as e:
            logger.error(f"Error evaluating station {s.get('id')}: {e}")

    flagged_total = summary["warning"] + summary["undrinkable"]
    notifications_created = []
    push_summary = {"total_sent": 0, "total_failed": 0}
    suppressed_stations: List[Dict[str, Any]] = []
    alerted_station_ids: List[str] = []

    # Dispatch alerts ONLY for contaminated stations, max 1 push per station per day
    if notify_residents and flagged_by_barangay:
        alert_log = _load_push_alert_log()
        today = _today_str()

        for b_name, flagged_list in flagged_by_barangay.items():
            contaminated_items = [st for st in flagged_list if st.get("predicted_status") == "undrinkable"]
            if not contaminated_items:
                continue  # warnings only -> no resident notification

            if bypass_daily_limit:
                new_items = contaminated_items
            else:
                new_items = []
                for st in contaminated_items:
                    if alert_log.get(str(st.get("station_id"))) == today:
                        suppressed_stations.append({
                            "station_id": st.get("station_id"),
                            "station_name": st.get("station_name"),
                            "barangay": b_name,
                            "reason": "already_notified_today",
                        })
                    else:
                        new_items.append(st)

            if not new_items:
                logger.info(
                    f"[{triggered_by}] Brgy. {b_name}: {len(contaminated_items)} contaminated station(s) "
                    f"already notified today - push suppressed."
                )
                continue

            max_risk = max(st.get("risk_percentage", 0) for st in new_items)
            station_names = [st.get("station_name") or "Water Station" for st in new_items[:3]]
            names_summary = ", ".join(station_names)
            if len(new_items) > 3:
                names_summary += f" and {len(new_items) - 3} other(s)"

            title = f"🚨 Water Quality Alert: Contamination Risk in Brgy. {b_name}"
            message = (
                f"Machine learning surveillance detected high contamination probability (peak {max_risk}%) "
                f"for {len(new_items)} water source(s): {names_summary}. Precautionary water boiling is strongly advised."
            )

            # 1) In-app notification record (failure here must not block the push)
            notif_record: Dict[str, Any] = {
                "title": title,
                "message": message,
                "type": "critical",
                "barangay": b_name,
                "target_roles": ["resident", "barangay_official"],
                "created_by": current_user_id,  # NULL for background runs (FK -> users.id)
            }
            try:
                insert_res = sb.table("notifications").insert(notif_record).execute()
                if insert_res.data:
                    notif_record = insert_res.data[0]
            except Exception as notif_err:
                logger.error(f"[{triggered_by}] Error saving notification for Brgy. {b_name}: {notif_err}")

            # 2) Web Push
            sent_count = 0
            try:
                push_res = await broadcast_push_notification(
                    title=f"WaterWatch: {title}",
                    message=message,
                    barangay=b_name,
                    url="/portal/notifications",
                    tag=f"ml-scan-{b_name.lower().replace(' ', '-')}",
                )
                if isinstance(push_res, dict):
                    sent_count = push_res.get("sent", 0)
                    push_summary["total_failed"] += push_res.get("failed", 0)
                push_summary["total_sent"] += sent_count
            except Exception as push_err:
                logger.error(f"[{triggered_by}] Web Push error for Brgy. {b_name}: {push_err}")

            notif_record["pushed_count"] = sent_count
            notif_record["pushed_residents_count"] = sent_count
            notif_record["body"] = message
            notifications_created.append(notif_record)

            ids = [str(st.get("station_id")) for st in new_items]
            alerted_station_ids.extend(ids)
            for sid in ids:
                alert_log[sid] = today
            logger.info(
                f"[{triggered_by}] Brgy. {b_name}: alerted {len(new_items)} contaminated station(s), "
                f"{sent_count} push(es) sent."
            )

        if alerted_station_ids:
            _save_push_alert_log(alert_log)

    return {
        "barangay": target_b or "all",
        "total_evaluated": len(results),
        "evaluated_stations": len(results),
        "summary": summary,
        "safe_count": summary.get("safe", 0),
        "warning_count": summary.get("warning", 0),
        "contaminated_count": summary.get("undrinkable", 0),
        "flagged_total": flagged_total,
        "flagged_count": flagged_total,
        "notifications_count": len(notifications_created),
        "notifications_dispatched": notifications_created,
        "notifications": notifications_created,
        "push_stats": push_summary,
        "triggered_by": triggered_by,
        "alerted_station_ids": alerted_station_ids,
        "suppressed_duplicates": suppressed_stations,
        "suppressed_count": len(suppressed_stations),
        "flagged_stations": [
            {
                "id": st["station_id"],
                "station_id": st["station_id"],
                "name": st["station_name"],
                "station_name": st["station_name"],
                "barangay": st["barangay"],
                "risk_percentage": st["risk_percentage"],
                "predicted_status": st["predicted_status"],
                "status": st["predicted_status"],
                "risk_level": st["risk_level"],
                "risk_factors": st.get("risk_factors", []),
                "advisory": st.get("advisory", ""),
            }
            for b_list in flagged_by_barangay.values()
            for st in b_list
        ],
        "results": results,
        "timestamp": datetime.now().isoformat(),
    }
