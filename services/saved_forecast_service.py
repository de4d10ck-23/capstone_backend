"""Shared, durable forecasts. Reading the resident map never invokes the model."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.database import get_supabase
from services.forecast_service import get_active_config, load_model_payload, predict_station_risk

logger = logging.getLogger(__name__)
_refresh_lock = asyncio.Lock()
MANILA = timezone(timedelta(hours=8))


def read_all(query):
    rows = []
    while True:
        page = query.range(len(rows), len(rows) + 499).execute().data or []
        rows.extend(page)
        if len(page) < 500:
            return rows


async def refresh_saved_forecasts():
    """Refresh all eight dates; retain previous successes when any prediction fails."""
    async with _refresh_lock:
        started = datetime.now(timezone.utc)
        summary = {"started_at": started.isoformat(), "dates": 8, "saved": 0, "failed": 0}
        try:
            sb = get_supabase()
            # Fail early if the migration has not been applied.
            sb.table("water_forecasts").select("station_id").limit(1).execute()
            if "pipeline" not in load_model_payload():
                raise RuntimeError("Prediction model is unavailable")
            stations = read_all(sb.table("water_locations").select("*").is_("archived_at", "null").order("id"))
            config = get_active_config()
            today = started.astimezone(MANILA).date()
            # Group stations by weather grid so one provider request supplies all eight dates.
            groups = {}
            for station in stations:
                try:
                    key = (round(float(station["latitude"]), 2), round(float(station["longitude"]), 2))
                    groups.setdefault(key, []).append(station)
                except (KeyError, TypeError, ValueError):
                    summary["failed"] += 8
            semaphore = asyncio.Semaphore(4)

            async def compute_group(group):
                async with semaphore:
                    rows = []
                    for offset in range(8):
                        target = (today + timedelta(days=offset)).isoformat()
                        for station in group:
                            try:
                                prediction = await predict_station_risk(station["id"],
                                    forecast_date=target, station_data=station, config=config)
                                rows.append({"station_id": station["id"], "forecast_date": target,
                                    "prediction": prediction, "generated_at": started.isoformat()})
                            except Exception as exc:
                                summary["failed"] += 1
                                logger.warning("Forecast failed for %s on %s: %s", station["id"], target, exc)
                    # Small transactions keep old rows intact if a write fails.
                    for index in range(0, len(rows), 200):
                        batch = rows[index:index + 200]
                        sb.table("water_forecasts").upsert(batch, on_conflict="station_id,forecast_date").execute()
                        summary["saved"] += len(batch)

            outcomes = await asyncio.gather(*(compute_group(group) for group in groups.values()), return_exceptions=True)
            if any(isinstance(item, Exception) for item in outcomes):
                raise RuntimeError("Some forecasts could not be saved")
            sb.table("water_forecasts").delete().lt("forecast_date", today.isoformat()).execute()
            summary["status"] = "complete" if not summary["failed"] else "partial"
        except Exception:
            logger.exception("Saved forecast refresh failed")
            summary.update(status="failed", error="Forecast refresh failed. Check weather connectivity, model, and water_forecasts migration.")
        return summary


def get_saved_forecasts(forecast_date, barangay=None):
    from services.heatmap_service import get_run_interval_minutes

    sb = get_supabase()
    query = sb.table("water_locations").select("id,barangay").is_("archived_at", "null").order("id")
    if barangay:
        query = query.ilike("barangay", barangay.strip())
    stations = read_all(query)
    ids = {str(station["id"]) for station in stations}
    rows = read_all(sb.table("water_forecasts").select("*")
        .eq("forecast_date", forecast_date).order("station_id"))
    now = datetime.now(timezone.utc)
    max_age = timedelta(minutes=get_run_interval_minutes() + 5)
    results = []
    for row in rows:
        if str(row["station_id"]) not in ids:
            continue
        generated = datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00"))
        results.append({**row["prediction"], "generated_at": row["generated_at"],
                        "stale": now - generated > max_age})
    available = {str(result["station_id"]) for result in results}
    return {"forecast_date": forecast_date, "results": results,
        "unavailable_station_ids": sorted(ids - available), "timezone": "Asia/Manila",
        "storage": "saved", "stale_count": sum(item["stale"] for item in results),
        "oldest_generated_at": min((item["generated_at"] for item in results), default=None)}
