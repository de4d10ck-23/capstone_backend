import logging
from typing import Annotated
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, get_optional_user, require_admin_or_inspector, require_water_location_editor
from core.storage import upload_to_supabase
from models.water_location import WaterLocationCreate, WaterLocationUpdate
from services.push_service import broadcast_push_notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/water-locations", tags=["Water Locations"])

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def normalize_source_type(value):
    value = (value or "unknown").strip().lower()
    if value == "piped_water":
        value = "piped_tap"
    if value not in {"unknown", "deep_well", "spring", "piped_tap", "shallow_well", "refilling_station"}:
        raise HTTPException(422, "Select a valid water source type.")
    return value


@router.get("", include_in_schema=False)
@router.get("/")
async def list_water_locations(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Get all water locations with optional barangay filtering."""
    sb = get_supabase()
    query = sb.table("water_locations").select("*").is_("archived_at", "null")

    # Scoped filtering for barangay official or query parameter
    if current_user and current_user.get("role") == "barangay_official":
        if not (current_user.get("barangay") or "").strip():
            raise HTTPException(403, "Your account has no assigned barangay. Contact an administrator.")
        query = query.ilike("barangay", current_user["barangay"].strip())
    elif barangay:
        query = query.ilike("barangay", barangay.strip())

    result = query.order("created_at", desc=True).execute()
    locations = []
    for loc in (result.data or []):
        name_val = loc.get("full_name") or loc.get("name") or "Water Station"
        loc["name"] = name_val
        loc["full_name"] = name_val
        loc["source_type"] = loc.get("source_type") or loc.get("type") or "unknown"
        loc["type"] = loc["source_type"]
        if not loc.get("status"):
            loc["status"] = _compute_water_status(loc)
        locations.append(loc)

    return {"success": True, "data": locations}


@router.get("/public")
async def list_public_water_locations():
    """Public view — no auth required. Returns safe subset of fields."""
    sb = get_supabase()
    result = (
        sb.table("water_locations")
        .select("id, full_name, barangay, latitude, longitude, coliform_bacteria, e_coli, bacteriological_exam, sample_date, sample_time, image_url, notes")
        .is_("archived_at", "null")
        .order("created_at", desc=True)
        .execute()
    )

    # Add computed water_status
    locations = []
    for loc in (result.data or []):
        name_val = loc.get("full_name") or loc.get("name") or "Water Station"
        loc["name"] = name_val
        loc["full_name"] = name_val
        status = _compute_water_status(loc)
        loc["water_status"] = status
        loc["status"] = status
        locations.append(loc)

    return {"success": True, "data": locations}


@router.get("/archive")
async def list_archived_water_locations(
    current_user: Annotated[dict, Depends(require_water_location_editor)],
):
    """Archived stations are visible only to editors in their jurisdiction."""
    query = get_supabase().table("water_locations").select("*").not_.is_("archived_at", "null")
    if current_user.get("role") == "barangay_official":
        barangay = (current_user.get("barangay") or "").strip()
        if not barangay:
            raise HTTPException(403, "Your account has no assigned barangay.")
        query = query.ilike("barangay", barangay)
    try:
        rows = query.order("archived_at", desc=True).execute().data or []
    except Exception as exc:
        logger.exception("Could not load archived water sources")
        raise HTTPException(503, "Water source archive is unavailable. Apply the water source archive migration.") from exc
    for row in rows:
        row["name"] = row.get("full_name") or "Water Station"
    return {"success": True, "data": rows}



@router.get("/{location_id}")
async def get_water_location(
    location_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Get a specific water location."""
    sb = get_supabase()
    result = sb.table("water_locations").select("*").eq("id", location_id).is_("archived_at", "null").limit(1).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "data": result.data[0]}
 
 
@router.get("/{location_id}/history")
async def get_water_location_history(
    location_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Read dated snapshots without mutating the station."""
    sb = get_supabase()
    location = sb.table("water_locations").select("id, barangay").eq("id", location_id).is_("archived_at", "null").limit(1).execute()
    if not location.data:
        raise HTTPException(404, "Water location not found")
    if _user.get("role") == "barangay_official":
        if not (_user.get("barangay") or "").strip() or (_user.get("barangay") or "").strip().lower() != (location.data[0].get("barangay") or "").strip().lower():
            raise HTTPException(403, "You can only view history in your assigned barangay.")
    try:
        logs = []
        while True:
            page = (sb.table("water_status_logs").select("*").eq("location_id", location_id)
                .order("effective_date").order("recorded_at").order("id")
                .range(len(logs), len(logs) + 499).execute().data or [])
            logs.extend(page)
            if len(page) < 500:
                break
    except Exception:
        logger.exception("Could not read structured water history")
        raise HTTPException(503, "Status history is unavailable. Check the water status history migration.")
    return {"success": True, "data": logs, "latest": logs[-1] if logs else None}


@router.post("", include_in_schema=False)
@router.post("/")
async def create_water_location(
    body: WaterLocationCreate,
    current_user: Annotated[dict, Depends(require_water_location_editor)],
):
    """Create a new water location. Admin, Inspector, or Barangay Official."""
    sb = get_supabase()

    # If barangay official, enforce their assigned barangay
    if current_user.get("role") == "barangay_official":
        user_brgy = (current_user.get("barangay") or "").strip()
        if not user_brgy:
            raise HTTPException(status_code=400, detail="Your user profile does not have an assigned barangay.")
        body.barangay = user_brgy

    # Validate Maasin bounds
    if not (10.0 <= body.latitude <= 10.3) or not (124.7 <= body.longitude <= 125.1):
        raise HTTPException(status_code=400, detail="Coordinates must be within Maasin City bounds (Latitude: 10.0 - 10.3, Longitude: 124.7 - 125.1)")

    # Normalize bacteriological_exam
    exam = (body.bacteriological_exam or "").strip().lower()
    if exam and exam not in ("passed", "failed", "untested"):
        exam = "untested"

    full_name = (body.full_name or body.name or "Water Station").strip()
    status_val = (body.status or "safe").strip().lower()
    if not exam or exam == "untested":
        exam = "passed" if status_val == "safe" else "failed"

    notes_val = body.notes or body.description or body.remarks or None

    new_loc = {
        "source_type": normalize_source_type(body.source_type or body.type),
        "full_name": full_name,
        "barangay": body.barangay,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "coliform_bacteria": None if status_val == "unknown" else (body.coliform_bacteria if body.coliform_bacteria is not None else ((body.coliform_count or 0) > 0)),
        "e_coli": None if status_val == "unknown" else (body.e_coli if body.e_coli is not None else ((body.e_coli_count or 0) > 0)),
        "bacteriological_exam": "untested" if status_val == "unknown" else exam,
        "image_url": body.image_url,
        "sample_date": body.sample_date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
        "sample_time": body.sample_time or datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S"),
        "created_by": current_user["id"],
        "inspector_id": current_user["id"] if current_user["role"] == "sanitization_inspector" else None,
        "status": status_val,
        "notes": notes_val,
    }

    result = sb.table("water_locations").insert(new_loc).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create water location")

    # Registration history is saved atomically by the database trigger.

    try:
        import asyncio
        from services.heatmap_service import recompute_heatmaps
        asyncio.create_task(recompute_heatmaps("station_created"))
    except Exception as recompute_err:
        logger.debug(f"Could not trigger heatmap recompute: {recompute_err}")

    return {"success": True, "message": "Water location created successfully", "data": result.data[0]}


@router.put("/{location_id}", include_in_schema=False)
@router.put("/{location_id}/")
async def update_water_location(
    location_id: str,
    body: WaterLocationUpdate,
    current_user: Annotated[dict, Depends(require_water_location_editor)],
):
    """Update a water location. Admin, Inspector, or Barangay Official."""
    sb = get_supabase()

    # Fetch existing state for transition audit
    existing = None
    try:
        existing = sb.table("water_locations").select("*").eq("id", location_id).is_("archived_at", "null").limit(1).execute()
    except Exception as fetch_err:
        logger.warning(f"Could not fetch existing water location: {fetch_err}")
        raise HTTPException(503, "Water source lookup is unavailable. Please try again.") from fetch_err

    if not existing or not existing.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    # If barangay official, ensure they can only edit their own barangay
    if current_user.get("role") == "barangay_official":
        user_brgy = (current_user.get("barangay") or "").strip().lower()
        existing_brgy = (existing.data[0].get("barangay") or "").strip().lower()
        if not user_brgy or existing_brgy != user_brgy:
            raise HTTPException(status_code=403, detail="You can only edit water stations in your assigned barangay.")
        body.barangay = existing.data[0].get("barangay")

    update_data = body.model_dump(exclude_unset=True)
    history_date = update_data.pop("history_date", None) or update_data.get("sample_date") or datetime.now(timezone(timedelta(hours=8))).date().isoformat()

    # Validate coordinates if provided
    if "latitude" in update_data and not (10.0 <= update_data["latitude"] <= 10.3):
        raise HTTPException(status_code=400, detail="Latitude must be within Maasin City bounds")
    if "longitude" in update_data and not (124.7 <= update_data["longitude"] <= 125.1):
        raise HTTPException(status_code=400, detail="Longitude must be within Maasin City bounds")

    # Normalize bacteriological_exam
    if "bacteriological_exam" in update_data:
        raw = (update_data["bacteriological_exam"] or "").strip().lower()
        if raw in ("passed", "failed", "untested"):
            update_data["bacteriological_exam"] = raw
        elif raw == "":
            update_data["bacteriological_exam"] = None

    if "name" in update_data and not update_data.get("full_name"):
        update_data["full_name"] = update_data["name"]

    if "description" in update_data and "notes" not in update_data:
        update_data["notes"] = update_data["description"]

    # Clean non-column fields if present
    update_data.pop("name", None)
    update_data.pop("description", None)
    if "source_type" in update_data or "type" in update_data:
        update_data["source_type"] = normalize_source_type(update_data.get("source_type") or update_data.get("type"))
    update_data.pop("type", None)
    update_data.pop("remarks", None)
    update_data.pop("coliform_count", None)
    update_data.pop("e_coli_count", None)

    old_status = existing.data[0].get("status")
    try:
        result = sb.rpc("update_water_location_history", {
            "p_location_id": location_id, "p_patch": update_data,
            "p_effective_date": history_date, "p_actor": current_user["id"],
        }).execute()
    except Exception:
        logger.exception("Atomic water status history update failed")
        raise HTTPException(400, "Could not save status history. Check the selected day, sample date, and database history migration.")
    updated_station = result.data
    if isinstance(updated_station, list):
        updated_station = updated_station[0] if updated_station else None
    if not updated_station:
        raise HTTPException(500, "Water location update returned no data")
    new_status = updated_station.get("status")

    # Automatically dispatch Boil Water advisory if water source transitioned to contaminated
    if new_status in ("contaminated", "undrinkable") and old_status not in ("contaminated", "undrinkable"):
        try:
            st_name = updated_station.get("full_name") or updated_station.get("name") or "Water Source"
            target_brgy = updated_station.get("barangay")
            auto_notif = {
                "title": f"⚠️ Boil Water Notice: {st_name}",
                "message": f"Water source '{st_name}' in Brgy. {target_brgy} has been flagged as CONTAMINATED. Residents are strongly advised to boil water before drinking or use alternative safe stations.",
                "type": "critical",
                "barangay": target_brgy,
                "target_roles": ["resident", "barangay_official"],
                "created_by": current_user["id"],
            }
            n_res = sb.table("notifications").insert(auto_notif).execute()
            await broadcast_push_notification(
                title=auto_notif["title"],
                message=auto_notif["message"],
                barangay=target_brgy,
                url="/portal/notifications",
                tag=f"contam-alert-{location_id}",
            )
            logger.info(f"Dispatched automated Boil Water push notification for station {st_name} in {target_brgy}")
        except Exception as auto_notif_err:
            logger.warning(f"Could not dispatch automatic contamination notification: {auto_notif_err}")

    try:
        import asyncio
        from services.heatmap_service import recompute_heatmaps
        asyncio.create_task(recompute_heatmaps("station_status_updated"))
    except Exception as recompute_err:
        logger.debug(f"Could not trigger heatmap recompute: {recompute_err}")

    return {"success": True, "message": "Water location updated", "data": updated_station}



@router.delete("/{location_id}", include_in_schema=False)
@router.delete("/{location_id}/")
async def delete_water_location(
    location_id: str,
    current_user: Annotated[dict, Depends(require_water_location_editor)],
):
    """Archive a station without losing its ID or dependent records."""
    return await set_water_location_archive(location_id, current_user, True)


@router.post("/{location_id}/restore")
async def restore_water_location(
    location_id: str,
    current_user: Annotated[dict, Depends(require_water_location_editor)],
):
    return await set_water_location_archive(location_id, current_user, False)


async def set_water_location_archive(location_id: str, current_user: dict, archive: bool):
    try:
        result = get_supabase().rpc("set_water_source_archived", {
            "p_location_id": location_id,
            "p_actor_id": current_user["id"],
            "p_archive": archive,
        }).execute().data
    except Exception as exc:
        message = str(exc)
        if "Water source not found" in message:
            raise HTTPException(404, "Water source not found") from exc
        if "assigned barangay" in message:
            raise HTTPException(403, "You can only manage water sources in your assigned barangay.") from exc
        logger.exception("Could not change water source archive state")
        raise HTTPException(503, "Could not update the water source archive. Apply the archive migration and retry.") from exc
    station = result[0] if isinstance(result, list) and result else result
    if not station:
        raise HTTPException(500, "The archive operation returned no station.")
    try:
        import asyncio
        from services.heatmap_service import recompute_heatmaps
        asyncio.create_task(recompute_heatmaps("station_archived" if archive else "station_restored"))
    except Exception as recompute_err:
        logger.debug("Could not trigger heatmap recompute: %s", recompute_err)
    return {"success": True, "message": "Water source archived" if archive else "Water source restored", "data": station}


@router.post("/upload-image", include_in_schema=False)
@router.post("/upload-image/")
async def upload_image(
    file: UploadFile = File(...),
    current_user: Annotated[dict, Depends(require_water_location_editor)] = None,
):
    """Upload an image for a water location to Supabase Storage."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file selected")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}")

    contents = await file.read()
    if len(contents) > 16 * 1024 * 1024:  # 16MB limit
        raise HTTPException(status_code=400, detail="File too large. Max 16MB.")

    public_url = await upload_to_supabase(contents, file.filename, file.content_type or "image/jpeg")

    return {"success": True, "message": "Image uploaded", "image_url": public_url}


def _compute_water_status(loc: dict) -> str:
    """Compute water status from location data (same logic as old system)."""
    exam = (loc.get("bacteriological_exam") or "").strip().lower()
    coliform = loc.get("coliform_bacteria")
    e_coli = loc.get("e_coli")

    if exam == "failed":
        return "contaminated"
    if exam == "passed":
        return "safe"
    if exam == "untested" and (coliform is True or e_coli is True):
        return "warning"
    if exam == "untested" and coliform is None and e_coli is None:
        return "not_tested"

    # Fallback to bacteria-only
    if e_coli is True:
        return "contaminated"
    if coliform is True and e_coli is False:
        return "warning"
    if coliform is False and e_coli is False:
        return "safe"

    return "not_tested"
