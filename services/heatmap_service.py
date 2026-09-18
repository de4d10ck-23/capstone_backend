import os
import math
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from core.database import get_supabase
from core.spatial import haversine_distance, calculate_station_hazard_distances
from ml.dataset import encode_source_type, count_buffer_encroachments
from services.forecast_service import load_model_payload, get_active_config
from services.weather_service import get_live_weather, MAASIN_DEFAULT_LAT, MAASIN_DEFAULT_LNG

logger = logging.getLogger(__name__)

# In-memory cache for computed heatmaps
_heatmap_cache: Dict[str, Any] = {
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
    },
}

_scheduler_task: Optional[asyncio.Task] = None


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
    Generate GeoJSON Point FeatureCollection representing all contamination hazards
    (points, lines, and polygons densified into weighted heat points).
    """
    sb = get_supabase()
    try:
        res = sb.table("contamination_hazards").select("*").execute()
        hazards = res.data or []
    except Exception as e:
        logger.warning(f"Error loading hazards for heatmap: {e}")
        hazards = []

    features = []
    for h in hazards:
        geom_type = (h.get("geometry_type") or "Point").lower()
        coords = h.get("coordinates")
        if not coords:
            continue

        weight = get_hazard_risk_weight(h.get("risk_level"), h.get("hazard_type"))
        base_props = {
            "hazard_id": h.get("id"),
            "name": h.get("name"),
            "hazard_type": h.get("hazard_type"),
            "risk_level": h.get("risk_level"),
            "barangay": h.get("barangay"),
            "weight": weight,
        }

        if geom_type == "point":
            if isinstance(coords, list) and len(coords) >= 2:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [float(coords[0]), float(coords[1])],
                    },
                    "properties": {**base_props, "source_geometry": "Point"},
                })

        elif "line" in geom_type:
            sampled = densify_line(coords, step_meters=35.0)
            for pt in sampled:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [pt[0], pt[1]],
                    },
                    "properties": {**base_props, "source_geometry": "LineString"},
                })

        elif "polygon" in geom_type:
            sampled = densify_polygon(coords, step_meters=45.0)
            for pt in sampled:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [pt[0], pt[1]],
                    },
                    "properties": {**base_props, "source_geometry": "Polygon"},
                })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


async def generate_water_contamination_heatmap_geojson() -> Tuple[Dict[str, Any], Dict[str, int]]:
    """
    Generate GeoJSON Point FeatureCollection representing microbial and ML contamination
    risk across all monitored water locations using high-performance vectorized batch inference.
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

        coord = (lng, lat)
        dist_dict = calculate_station_hazard_distances(coord, hazards)
        encroachments = count_buffer_encroachments(coord, hazards, 50.0)
        source_code = encode_source_type(loc.get("source_type") or "", loc.get("name") or loc.get("full_name") or "")

        vec = [
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
        valid_locs.append((loc, lng, lat, dist_dict, encroachments))
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

    for i, (loc, lng, lat, dist_dict, encroachments) in enumerate(valid_locs):
        risk_prob = round(probabilities[i], 4) if i < len(probabilities) else 0.1

        if risk_prob < thresh_low:
            predicted_status = "safe"
        elif risk_prob <= thresh_high:
            predicted_status = "warning"
        else:
            predicted_status = "undrinkable"

        stats[predicted_status] += 1

        coliform = float(loc.get("coliform_count") or 0)
        e_coli = float(loc.get("e_coli_count") or 0)
        has_bacteria = coliform > 0 or e_coli > 0

        if predicted_status == "undrinkable":
            heat_weight = max(0.85, risk_prob)
        elif predicted_status == "warning":
            heat_weight = max(0.45, risk_prob)
        else:
            # Safe water sources emit minimal heat so contamination hotspots stand out
            heat_weight = max(0.02, min(0.20, risk_prob * 0.25))

        if has_bacteria and heat_weight < 0.7:
            heat_weight = min(1.0, heat_weight + 0.25)

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "properties": {
                "station_id": loc.get("id"),
                "name": loc.get("name") or loc.get("full_name"),
                "barangay": loc.get("barangay"),
                "status": predicted_status,
                "risk_probability": risk_prob,
                "coliform_count": coliform,
                "e_coli_count": e_coli,
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
    Full recalculation of both Hazard Heatmap and Water Contamination Heatmap.
    Updates the in-memory cache and returns metadata.
    """
    global _heatmap_cache
    if _heatmap_cache["is_updating"]:
        logger.info("Heatmap calculation already in progress. Skipping concurrent run.")
        return _heatmap_cache

    _heatmap_cache["is_updating"] = True
    start_time = datetime.now()
    logger.info(f"Starting heatmap computation (triggered by: {triggered_by}) at {start_time.isoformat()}...")

    try:
        hazard_geojson = await generate_hazard_heatmap_geojson()
        water_geojson, stats = await generate_water_contamination_heatmap_geojson()

        now = datetime.now()
        next_run = now + timedelta(hours=1)

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
            "triggered_by": triggered_by,
        }
        logger.info(
            f"Heatmap computation completed in {(datetime.now() - start_time).total_seconds():.2f}s: "
            f"{_heatmap_cache['summary']['total_hazard_points']} hazard points, "
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


async def trigger_manual_heatmap_refresh() -> Dict[str, Any]:
    """Admin manual trigger to bypass the 1-hour wait and refresh immediately."""
    return await recompute_heatmaps(triggered_by="admin_manual")


async def hourly_heatmap_worker():
    """Background loop that recalculates heatmaps every 1 hour (3600 seconds)."""
    logger.info("Hourly heatmap background scheduler initialized.")
    # Initial run after server boot
    try:
        await asyncio.sleep(5)  # allow database connections to settle
        await recompute_heatmaps(triggered_by="startup_scheduler")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in initial scheduler run: {e}")

    while True:
        try:
            await asyncio.sleep(3600)  # 1 hour
            logger.info("Hourly timer fired: Executing scheduled heatmap recalculation...")
            await recompute_heatmaps(triggered_by="hourly_cron")
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
        loop = asyncio.get_event_loop()
        _scheduler_task = loop.create_task(hourly_heatmap_worker())
        logger.info("Started background task for hourly heatmap updates.")


def stop_heatmap_scheduler():
    """Stop the background scheduler task on shutdown."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        _scheduler_task = None
