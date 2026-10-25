import os
import json
import math
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

import shapely.geometry
from shapely.geometry import shape, Point

from core.database import get_supabase
from core.constants import get_barangay_population, MAX_BARANGAY_POPULATION
from core.spatial import haversine_distance
from ml.dataset import encode_source_type, get_station_hazard_levels
from services.forecast_service import load_model_payload, get_active_config
from services.weather_service import get_live_weather, MAASIN_DEFAULT_LAT, MAASIN_DEFAULT_LNG

logger = logging.getLogger(__name__)

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
DATA_DIR = os.path.join(BACKEND_ROOT, "data")

# In-memory cache for computed heatmaps
_heatmap_cache: Dict[str, Any] = {
    "flood_hazards": {"type": "FeatureCollection", "features": []},
    "landslide_hazards": {"type": "FeatureCollection", "features": []},
    "hazard_heatmap": {"type": "FeatureCollection", "features": []},
    "water_contamination_heatmap": {"type": "FeatureCollection", "features": []},
    "last_updated": None,
    "next_run": None,
    "is_updating": False,
    "summary": {
        "total_hazard_points": 0,
        "total_water_stations": 0,
        "contaminated_stations": 0,
        "warning_stations": 0,
        "safe_stations": 0,
        "flood_zones": 0,
        "landslide_zones": 0,
    },
}

_scheduler_task: Optional[asyncio.Task] = None
_reschedule_event: Optional[asyncio.Event] = None
_last_run_attempt: Optional[datetime] = None
_last_alert_run: Optional[Dict[str, Any]] = None
ALERT_HISTORY_PATH = os.path.join(DATA_DIR, "alert_run_history.json")

# Configurable background model re-run interval (minutes)
SCHEDULER_SETTINGS_PATH = os.path.join(DATA_DIR, "scheduler_settings.json")
DEFAULT_INTERVAL_MINUTES = 60
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440  # 24 hours


def get_run_interval_minutes() -> int:
    """Read the configured scheduler interval (minutes) from disk, falling back to default."""
    try:
        if os.path.exists(SCHEDULER_SETTINGS_PATH):
            with open(SCHEDULER_SETTINGS_PATH, "r", encoding="utf-8") as f:
                val = int(json.load(f).get("run_interval_minutes", DEFAULT_INTERVAL_MINUTES))
                return max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, val))
    except Exception as e:
        logger.warning(f"Could not read scheduler settings: {e}")
    return DEFAULT_INTERVAL_MINUTES


def set_run_interval_minutes(minutes: int) -> int:
    """Persist a new scheduler interval and wake the worker so it reschedules immediately."""
    minutes = int(minutes)
    if minutes < MIN_INTERVAL_MINUTES or minutes > MAX_INTERVAL_MINUTES:
        raise ValueError(
            f"Interval must be between {MIN_INTERVAL_MINUTES} and {MAX_INTERVAL_MINUTES} minutes."
        )
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SCHEDULER_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"run_interval_minutes": minutes}, f, indent=2)

    # Recalculate next_run from the last run using the new interval
    if _heatmap_cache.get("last_updated"):
        try:
            last = datetime.fromisoformat(_heatmap_cache["last_updated"])
            _heatmap_cache["next_run"] = (last + timedelta(minutes=minutes)).isoformat()
        except Exception:
            pass
    _heatmap_cache["run_interval_minutes"] = minutes

    if _reschedule_event is not None:
        _reschedule_event.set()
    logger.info(f"Background model re-run interval updated to {minutes} minute(s).")
    return minutes


def get_scheduler_status() -> Dict[str, Any]:
    """Return scheduler interval and timing info."""
    return {
        "run_interval_minutes": get_run_interval_minutes(),
        "min_interval_minutes": MIN_INTERVAL_MINUTES,
        "max_interval_minutes": MAX_INTERVAL_MINUTES,
        "last_updated": _heatmap_cache.get("last_updated"),
        "next_run": _heatmap_cache.get("next_run"),
        "is_updating": _heatmap_cache.get("is_updating", False),
        "is_running": _scheduler_task is not None and not _scheduler_task.done(),
        "last_alert_run": _last_alert_run,
    }


def load_official_hazard_datasets() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Load pre-processed official flood and landslide GeoJSONs derived from Maasin_LandFlood."""
    flood_path = os.path.join(DATA_DIR, "maasin_flood_hazards.json")
    landslide_path = os.path.join(DATA_DIR, "maasin_landslide_hazards.json")

    flood_data = {"type": "FeatureCollection", "features": []}
    landslide_data = {"type": "FeatureCollection", "features": []}

    if os.path.exists(flood_path):
        try:
            with open(flood_path, "r", encoding="utf-8") as f:
                flood_data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load {flood_path}: {e}")

    if os.path.exists(landslide_path):
        try:
            with open(landslide_path, "r", encoding="utf-8") as f:
                landslide_data = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load {landslide_path}: {e}")

    return flood_data, landslide_data


def densify_line(coords: List[List[float]], step_meters: float = 30.0) -> List[Tuple[float, float]]:
    """
    Interpolate points along a LineString at approximately step_meters interval.
    coords: [[lng, lat], [lng, lat], ...]
    Returns list of (lng, lat) tuples.
    """
    if not coords:
        return []
    if len(coords) == 1:
        return [(coords[0][0], coords[0][1])]

    densified = []
    for i in range(len(coords) - 1):
        p1 = (float(coords[i][0]), float(coords[i][1]))
        p2 = (float(coords[i + 1][0]), float(coords[i + 1][1]))
        densified.append(p1)

        dist = haversine_distance(p1, p2)
        if dist > step_meters:
            num_steps = int(math.ceil(dist / step_meters))
            for s in range(1, num_steps):
                t = s / num_steps
                lng = p1[0] + t * (p2[0] - p1[0])
                lat = p1[1] + t * (p2[1] - p1[1])
                densified.append((lng, lat))

    densified.append((float(coords[-1][0]), float(coords[-1][1])))
    return densified


def densify_polygon(coords: Any, step_meters: float = 40.0) -> List[Tuple[float, float]]:
    """
    Densify polygon exterior ring and sample interior points (centroid & radial midpoints).
    coords: [[[lng, lat], ...]]
    """
    if not coords or not isinstance(coords, list) or len(coords) == 0:
        return []

    ring = coords[0] if isinstance(coords[0], list) and len(coords[0]) > 0 and isinstance(coords[0][0], (list, tuple)) else coords
    if not ring or len(ring) < 3:
        return []

    # 1. Densify exterior boundary
    boundary_points = densify_line(ring, step_meters=step_meters)

    # 2. Centroid calculation
    lngs = [float(p[0]) for p in ring]
    lats = [float(p[1]) for p in ring]
    centroid = (sum(lngs) / len(lngs), sum(lats) / len(lats))

    internal_points = [centroid]
    # Sample interior midpoints between centroid and boundary vertices
    for p in ring[::2]:
        p_tuple = (float(p[0]), float(p[1]))
        mid = ((centroid[0] + p_tuple[0]) / 2.0, (centroid[1] + p_tuple[1]) / 2.0)
        internal_points.append(mid)

    return boundary_points + internal_points


def get_hazard_risk_weight(risk_level: str, hazard_type: str) -> float:
    """Calculate normalized heat weight (0.1 to 1.0) for a hazard."""
    base = 0.7
    rl = (risk_level or "").lower()
    if "high" in rl or "critical" in rl:
        base = 1.0
    elif "medium" in rl or "moderate" in rl:
        base = 0.65
    elif "low" in rl:
        base = 0.35

    ht = (hazard_type or "").lower()
    if any(k in ht for k in ["latrine", "septic", "dumpsite", "piggery", "carcass"]):
        type_mult = 1.0
    elif any(k in ht for k in ["river", "stream", "drainage", "canal"]):
        type_mult = 0.85
    elif any(k in ht for k in ["farmland", "ag", "agricultural", "field"]):
        type_mult = 0.75
    else:
        type_mult = 0.8

    return round(min(1.0, max(0.1, base * type_mult)), 2)


async def generate_hazard_heatmap_geojson() -> Dict[str, Any]:
    """
    Generate unified GeoJSON FeatureCollection representing official Maasin Flood and Landslide
    hazard susceptibility zones loaded from Maasin_LandFlood (DENR-MGB official study area).
    """
    flood_data, landslide_data = load_official_hazard_datasets()
    combined_features = []

    for f in flood_data.get("features", []):
        combined_features.append(f)

    for f in landslide_data.get("features", []):
        combined_features.append(f)

    return {
        "type": "FeatureCollection",
        "metadata": {
            "source": "Maasin_LandFlood (DENR-MGB Study Area)",
            "total_flood_zones": len(flood_data.get("features", [])),
            "total_landslide_zones": len(landslide_data.get("features", [])),
            "total_features": len(combined_features),
        },
        "features": combined_features,
    }


async def generate_water_contamination_heatmap_geojson() -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Generate GeoJSON Point FeatureCollection representing microbial and ML contamination
    risk across all monitored water locations using high-performance vectorized batch inference
    augmented with official spatial hazard zone vulnerability weighting.
    """
    sb = get_supabase()
    try:
        res_loc = sb.table("water_locations").select("*").execute()
        locations = res_loc.data or []
    except Exception as e:
        logger.warning(f"Error loading water locations for heatmap: {e}")
        locations = []

    try:
        res_haz = sb.table("contamination_hazards").select("*").execute()
        hazards = res_haz.data or []
    except Exception as e:
        logger.warning(f"Error loading hazards for heatmap: {e}")
        hazards = []

    # Load official GIS hazard geometries for spatial vulnerability intersection
    flood_data, landslide_data = load_official_hazard_datasets()
    flood_shapes = []
    for f in flood_data.get("features", []):
        if f.get("geometry"):
            try:
                flood_shapes.append((shape(f["geometry"]), f.get("properties", {})))
            except Exception as e:
                logger.debug(f"Invalid flood geometry: {e}")

    landslide_shapes = []
    for f in landslide_data.get("features", []):
        if f.get("geometry"):
            try:
                landslide_shapes.append((shape(f["geometry"]), f.get("properties", {})))
            except Exception as e:
                logger.debug(f"Invalid landslide geometry: {e}")

    # Get regional weather once for inference
    weather_data = await get_live_weather(MAASIN_DEFAULT_LAT, MAASIN_DEFAULT_LNG)
    rf_24h = float(weather_data.get("rainfall_24h_mm") or 0.0)
    rf_48h = float(weather_data.get("rainfall_48h_mm") or (rf_24h * 1.5))
    temp = float(weather_data.get("temperature_celsius") or 28.0)
    humidity = float(weather_data.get("humidity_percent") or 75.0)

    # Get thresholds and model payload
    cfg = get_active_config()
    thresh_low = cfg.get("risk_threshold_low", 0.30)
    thresh_high = cfg.get("risk_threshold_high", 0.65)
    payload = load_model_payload()
    pipeline = payload.get("pipeline") if payload else None

    valid_locs = []
    feature_vectors = []

    for loc in locations:
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            continue
        try:
            lat = float(lat)
            lng = float(lng)
        except (ValueError, TypeError):
            continue

        pt = Point(lng, lat)
        flood_lvl = 0
        for s, p in flood_shapes:
            if s.contains(pt) or s.distance(pt) < 0.00045:
                code = p.get("code")
                if code == "HF":
                    flood_lvl = 2
                    break
                elif code == "MF" and flood_lvl < 1:
                    flood_lvl = 1

        landslide_lvl = 0
        for s, p in landslide_shapes:
            if s.contains(pt) or s.distance(pt) < 0.00045:
                code = p.get("code")
                if code == "HL":
                    landslide_lvl = 2
                    break
                elif code == "ML" and landslide_lvl < 1:
                    landslide_lvl = 1

        source_code = encode_source_type(loc.get("source_type") or "", loc.get("full_name") or loc.get("name") or "")

        vec = [
            flood_lvl,
            landslide_lvl,
            rf_24h,
            rf_48h,
            temp,
            humidity,
            source_code,
        ]
        valid_locs.append((loc, lng, lat, flood_lvl, landslide_lvl))
        feature_vectors.append(vec)

    # Batch inference
    probabilities = []
    if pipeline and feature_vectors:
        try:
            probas = pipeline.predict_proba(feature_vectors)
            probabilities = [float(p[1]) for p in probas]
        except Exception as inf_err:
            logger.error(f"Batch predict_proba error: {inf_err}")
            probabilities = [0.1] * len(valid_locs)
    else:
        probabilities = [0.1] * len(valid_locs)

    features = []
    stats = {"safe": 0, "warning": 0, "undrinkable": 0, "total": len(valid_locs)}

    # Evaluate each station with official environmental hazard vulnerability weighting
    for i, (loc, lng, lat, flood_lvl, landslide_lvl) in enumerate(valid_locs):
        base_prob = round(probabilities[i], 4) if i < len(probabilities) else 0.1
        pt = Point(lng, lat)

        # Check intersection with official flood susceptibility zones (~50m buffer)
        in_flood = None
        for s, p in flood_shapes:
            if s.contains(pt) or s.distance(pt) < 0.00045:
                in_flood = p
                break

        # Check intersection with official landslide susceptibility zones (~50m buffer)
        in_landslide = None
        for s, p in landslide_shapes:
            if s.contains(pt) or s.distance(pt) < 0.00045:
                in_landslide = p
                break

        # Calculate hazard vulnerability weight multiplier
        hazard_multiplier = 1.0
        hazard_factors = []

        if in_flood:
            code = in_flood.get("code", "")
            if code == "HF":
                hazard_multiplier += 0.30
                hazard_factors.append("High Flood Susceptibility")
            elif code == "MF":
                hazard_multiplier += 0.15
                hazard_factors.append("Moderate Flood Susceptibility")
            else:
                hazard_multiplier += 0.05
                hazard_factors.append("Low Flood Susceptibility")

        if in_landslide:
            code = in_landslide.get("code", "")
            if code == "HL":
                hazard_multiplier += 0.25
                hazard_factors.append("High Landslide Susceptibility")
            elif code == "ML":
                hazard_multiplier += 0.12
                hazard_factors.append("Moderate Landslide Susceptibility")

        # Environmental weighting amplifies risk during rain/runoff events
        vulnerability_weight = round(hazard_multiplier - 1.0, 3)
        if rf_24h > 1.0 or rf_48h > 3.0:
            risk_prob = min(0.99, base_prob * hazard_multiplier)
        elif vulnerability_weight > 0.1:
            risk_prob = min(0.99, base_prob * (1.0 + vulnerability_weight * 0.5))
        else:
            risk_prob = base_prob

        risk_prob = round(risk_prob, 4)

        # DIRECT SANITATION INSPECTOR STATUS:
        # Ground-truth status recorded by sanitation inspector and certified laboratory exams
        raw_status = (loc.get("status") or "").strip().lower()
        exam = (loc.get("bacteriological_exam") or "").strip().lower()

        coliform_val = loc.get("coliform_bacteria") if loc.get("coliform_bacteria") is not None else loc.get("coliform_count")
        e_coli_val = loc.get("e_coli") if loc.get("e_coli") is not None else loc.get("e_coli_count")
        has_coliform = coliform_val is True or (isinstance(coliform_val, (int, float)) and coliform_val > 0)
        has_ecoli = e_coli_val is True or (isinstance(e_coli_val, (int, float)) and e_coli_val > 0)

        if raw_status in ("contaminated", "undrinkable") or exam == "failed" or has_ecoli:
            final_status = "undrinkable"
        elif raw_status == "warning" or has_coliform:
            final_status = "warning"
        elif raw_status == "safe" or exam == "passed":
            final_status = "safe"
        else:
            final_status = "safe"

        stats[final_status] += 1

        # Barangay Population Exposure Weighting
        bgy = loc.get("barangay")
        bgy_pop = get_barangay_population(bgy)
        # Normalized population factor (ranges from ~0.04 in Santa Rosa to 1.0 in Combado)
        pop_factor = min(1.0, max(0.04, bgy_pop / float(MAX_BARANGAY_POPULATION)))

        # Heat intensity and plume size strictly driven by Inspector Status + Barangay Population
        if final_status == "undrinkable":
            # Inspector verified contaminated: intense crimson (0.75 - 1.0) and broad plume (150m - 400m)
            heat_weight = min(1.0, 0.75 + (0.25 * pop_factor))
            radius_scale = round(0.80 + (1.20 * pop_factor), 2)
            exposure_radius_m = int(150 + (250 * pop_factor))
        elif final_status == "warning":
            # Inspector verified warning: amber/orange heat (0.40 - 0.70) and moderate plume (90m - 250m)
            heat_weight = min(0.70, 0.40 + (0.30 * pop_factor))
            radius_scale = round(0.55 + (0.75 * pop_factor), 2)
            exposure_radius_m = int(90 + (160 * pop_factor))
        else:
            # Inspector verified safe: clean, minimal footprint
            heat_weight = 0.02
            radius_scale = 0.35
            exposure_radius_m = 40

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "properties": {
                "station_id": loc.get("id"),
                "name": loc.get("full_name") or loc.get("name") or "Water Station",
                "barangay": bgy,
                "barangay_population": bgy_pop,
                "population_exposure_factor": round(pop_factor, 3),
                "radius_scale": radius_scale,
                "exposure_radius_meters": exposure_radius_m,
                "status": final_status,
                "inspector_status": raw_status or final_status,
                "bacteriological_exam": exam or "None",
                "base_probability": base_prob,
                "risk_probability": risk_prob,
                "vulnerability_multiplier": round(hazard_multiplier, 2),
                "vulnerability_factors": hazard_factors,
                "in_flood_zone": in_flood is not None,
                "flood_risk_level": in_flood.get("risk_level", "None") if in_flood else "None",
                "in_landslide_zone": in_landslide is not None,
                "landslide_risk_level": in_landslide.get("risk_level", "None") if in_landslide else "None",
                "coliform_count": float(coliform_val or 0) if isinstance(coliform_val, (int, float)) else (1.0 if has_coliform else 0.0),
                "e_coli_count": float(e_coli_val or 0) if isinstance(e_coli_val, (int, float)) else (1.0 if has_ecoli else 0.0),
                "weight": round(heat_weight, 4),
            },
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }
    return geojson, stats


async def recompute_heatmaps(triggered_by: str = "schedule") -> Dict[str, Any]:
    """
    Full recalculation of official Environmental Hazard Layers and Water Contamination Heatmap.
    Updates the in-memory cache and returns metadata.
    """
    global _heatmap_cache
    if _heatmap_cache["is_updating"]:
        logger.info("Heatmap calculation already in progress. Skipping concurrent run.")
        return _heatmap_cache

    _heatmap_cache["is_updating"] = True
    start_time = datetime.now()
    global _last_run_attempt
    _last_run_attempt = start_time
    logger.info(f"Starting heatmap computation (triggered by: {triggered_by}) at {start_time.isoformat()}...")

    try:
        flood_data, landslide_data = load_official_hazard_datasets()
        hazard_geojson = await generate_hazard_heatmap_geojson()
        water_geojson, stats = await generate_water_contamination_heatmap_geojson()

        now = datetime.now()
        interval_minutes = get_run_interval_minutes()
        next_run = now + timedelta(minutes=interval_minutes)
        _heatmap_cache["run_interval_minutes"] = interval_minutes

        _heatmap_cache["flood_hazards"] = flood_data
        _heatmap_cache["landslide_hazards"] = landslide_data
        _heatmap_cache["hazard_heatmap"] = hazard_geojson
        _heatmap_cache["water_contamination_heatmap"] = water_geojson
        _heatmap_cache["last_updated"] = now.isoformat()
        _heatmap_cache["next_run"] = next_run.isoformat()
        _heatmap_cache["summary"] = {
            "total_hazard_points": len(hazard_geojson.get("features", [])),
            "total_water_stations": stats.get("total", 0),
            "contaminated_stations": stats.get("undrinkable", 0),
            "warning_stations": stats.get("warning", 0),
            "safe_stations": stats.get("safe", 0),
            "flood_zones": len(flood_data.get("features", [])),
            "landslide_zones": len(landslide_data.get("features", [])),
            "triggered_by": triggered_by,
        }
        logger.info(
            f"Heatmap computation completed in {(datetime.now() - start_time).total_seconds():.2f}s: "
            f"{_heatmap_cache['summary']['flood_zones']} flood zones, "
            f"{_heatmap_cache['summary']['landslide_zones']} landslide zones, "
            f"{_heatmap_cache['summary']['total_water_stations']} water stations evaluated."
        )
    except Exception as e:
        logger.error(f"Error during heatmap computation: {e}", exc_info=True)
    finally:
        _heatmap_cache["is_updating"] = False

    return _heatmap_cache


async def get_heatmaps_data() -> Dict[str, Any]:
    """Return cached heatmap datasets or trigger initial computation if cache is empty."""
    global _heatmap_cache
    if not _heatmap_cache["last_updated"]:
        await recompute_heatmaps(triggered_by="initial_load")
    return _heatmap_cache


async def run_resident_alert_scan(triggered_by: str) -> Dict[str, Any]:
    """
    Run citywide ML surveillance and push alerts to residents for CONTAMINATED sources only.
    Each water source is pushed at most once per day (enforced in forecast_service).
    """
    global _last_alert_run
    from services.forecast_service import run_barangay_surveillance_and_alert

    started = datetime.now()
    try:
        res = await run_barangay_surveillance_and_alert(
            barangay="all",
            notify_residents=True,
            current_user_id=None,
            triggered_by=triggered_by,
        )
        entry = {
            "ran_at": started.isoformat(),
            "triggered_by": triggered_by,
            "evaluated": res.get("total_evaluated", 0),
            "contaminated": res.get("contaminated_count", 0),
            "warning": res.get("warning_count", 0),
            "newly_alerted_stations": len(res.get("alerted_station_ids", [])),
            "suppressed_duplicates": res.get("suppressed_count", 0),
            "notifications": res.get("notifications_count", 0),
            "push_sent": res.get("push_stats", {}).get("total_sent", 0),
            "push_failed": res.get("push_stats", {}).get("total_failed", 0),
            "error": None,
        }
    except Exception as e:
        logger.error(f"[{triggered_by}] Resident alert scan failed: {e}", exc_info=True)
        entry = {"ran_at": started.isoformat(), "triggered_by": triggered_by, "error": str(e)}

    _last_alert_run = entry
    _append_alert_history(entry)
    logger.info(f"[{triggered_by}] Resident alert scan: {entry}")
    return entry


def _append_alert_history(entry: Dict[str, Any]) -> None:
    try:
        history: List[Dict[str, Any]] = []
        if os.path.exists(ALERT_HISTORY_PATH):
            with open(ALERT_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f) or []
        history.insert(0, entry)
        with open(ALERT_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history[:20], f, indent=2)
    except Exception as e:
        logger.warning(f"Could not write alert history: {e}")


async def run_model_cycle(triggered_by: str) -> Dict[str, Any]:
    """One full engine cycle: recompute heatmaps, then contaminated-only resident alerts."""
    result = await recompute_heatmaps(triggered_by=triggered_by)
    await run_resident_alert_scan(triggered_by=triggered_by)
    return result


async def trigger_manual_heatmap_refresh() -> Dict[str, Any]:
    """Admin manual trigger to bypass the scheduled wait and refresh immediately."""
    result = await run_model_cycle(triggered_by="admin_manual")
    # Restart the countdown from this manual run
    if _reschedule_event is not None:
        _reschedule_event.set()
    return result


async def _sleep_until_next_run() -> bool:
    """
    Sleep until the next scheduled run based on the configured interval.
    Returns True when it's time to run, False if woken early by an interval change
    (the caller should then re-evaluate the remaining wait time).
    """
    global _reschedule_event
    if _reschedule_event is None:
        _reschedule_event = asyncio.Event()

    interval_seconds = get_run_interval_minutes() * 60
    remaining = interval_seconds
    if _last_run_attempt is not None:
        elapsed = (datetime.now() - _last_run_attempt).total_seconds()
        remaining = max(0.0, interval_seconds - elapsed)

    _reschedule_event.clear()
    try:
        await asyncio.wait_for(_reschedule_event.wait(), timeout=remaining)
        return False  # interval changed -> recompute remaining wait
    except asyncio.TimeoutError:
        return True


async def hourly_heatmap_worker():
    """Background loop that recalculates heatmaps on the admin-configured interval."""
    global _reschedule_event
    _reschedule_event = asyncio.Event()
    logger.info(
        f"Heatmap background scheduler initialized (interval: {get_run_interval_minutes()} min)."
    )
    # Initial run after server boot
    try:
        await asyncio.sleep(5)  # allow database connections to settle
        await run_model_cycle(triggered_by="startup_scheduler")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in initial scheduler run: {e}")

    while True:
        try:
            should_run = await _sleep_until_next_run()
            if not should_run:
                continue
            logger.info(
                f"Scheduled timer fired ({get_run_interval_minutes()} min): Running model cycle (heatmaps + resident alerts)..."
            )
            await run_model_cycle(triggered_by="scheduled_cron")
        except asyncio.CancelledError:
            logger.info("Hourly heatmap scheduler cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in hourly heatmap scheduler loop: {e}")
            await asyncio.sleep(60)


def start_heatmap_scheduler():
    """Start the background scheduler task."""
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        try:
            _scheduler_task = asyncio.create_task(hourly_heatmap_worker())
            logger.info("Started background task for hourly heatmap updates.")
        except Exception as e:
            logger.warning(f"Could not initialize heatmap scheduler background task: {e}")


def stop_heatmap_scheduler():
    """Stop the background scheduler task on shutdown."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
