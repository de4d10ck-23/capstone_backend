import logging
from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, get_optional_user, require_admin_or_inspector, require_water_location_editor
from core.storage import upload_to_supabase, delete_from_supabase
from models.water_location import WaterLocationCreate, WaterLocationUpdate
from services.push_service import broadcast_push_notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/water-locations", tags=["Water Locations"])

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


@router.get("", include_in_schema=False)
@router.get("/")
async def list_water_locations(
    barangay: str | None = None,
    current_user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """Get all water locations with optional barangay filtering."""
    sb = get_supabase()
    query = sb.table("water_locations").select("*")

    # Scoped filtering for barangay official or query parameter
    if current_user and current_user.get("role") == "barangay_official" and current_user.get("barangay"):
        query = query.ilike("barangay", current_user["barangay"].strip())
    elif barangay:
        query = query.ilike("barangay", barangay.strip())

    result = query.order("created_at", desc=True).execute()
    locations = []
    for loc in (result.data or []):
        name_val = loc.get("full_name") or loc.get("name") or "Water Station"
        loc["name"] = name_val
        loc["full_name"] = name_val
        loc["source_type"] = loc.get("source_type") or loc.get("type") or "deep_well"
        loc["type"] = loc.get("type") or loc.get("source_type") or "deep_well"
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



@router.get("/{location_id}")
async def get_water_location(
    location_id: str,
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Get a specific water location."""
    sb = get_supabase()
    result = sb.table("water_locations").select("*").eq("id", location_id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "data": result.data}
 
 
@router.get("/{location_id}/history")
async def get_water_location_history(
    location_id: str,
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Fetch status audit timeline logs for a water location."""
    sb = get_supabase()
    logs = []
    try:
        res = sb.table("water_status_logs").select("*").eq("location_id", location_id).order("recorded_at", desc=False).execute()
        logs = res.data or []
    except Exception as exc:
        logger.warning(f"Error fetching water_status_logs: {exc}")
        logs = []

    # If no logs exist yet, build baseline entry from water_locations
    if not logs:
        try:
            loc = sb.table("water_locations").select("*").eq("id", location_id).single().execute()
            if loc.data:
                s_date = loc.data.get("sample_date")
                s_time = loc.data.get("sample_time") or "12:00:00"
                formatted_time = s_time if len(s_time) == 8 else f"{s_time}:00" if len(s_time) == 5 else "12:00:00"
                rec_at = f"{s_date}T{formatted_time}+08:00" if s_date else (loc.data.get("created_at") or loc.data.get("updated_at") or datetime.now(timezone.utc).isoformat())
                loc_status = (loc.data.get("status") or "safe").lower()
                loc_exam = loc.data.get("bacteriological_exam")
                if not loc_exam or loc_status == "unknown":
                    loc_exam = "untested" if loc_status == "unknown" else ("failed" if loc_status in ("undrinkable", "contaminated") else "passed")
                logs = [{
                    "id": f"baseline-{location_id}",
                    "location_id": location_id,
                    "status": loc_status,
                    "previous_status": None,
                    "bacteriological_exam": loc_exam,
                    "coliform_count": 1 if loc.data.get("coliform_bacteria") else 0,
                    "e_coli_count": 1 if loc.data.get("e_coli") else 0,
                    "notes": "Initial water source registration (Pending safety evaluation)" if loc_status == "unknown" else "Initial water source registration",
                    "recorded_at": rec_at,
                    "sample_date": s_date,
                    "sample_time": s_time,
                }]
        except Exception as exc:
            logger.warning(f"Error generating baseline status log: {exc}")

    # Standardize sample_date and sample_time on each log to avoid local timezone offset shifting
    for log in logs:
        rec = log.get("recorded_at") or ""
        if "T" in rec:
            log["sample_date"] = rec.split("T")[0]
            time_part = rec.split("T")[1].replace("Z", "").split("+")[0]
            log["sample_time"] = time_part[:8] if len(time_part) >= 8 else time_part

    # Identify the chronologically latest audit log
    def get_sort_key(item):
        s_d = item.get("sample_date") or ""
        s_t = item.get("sample_time") or "00:00:00"
        return f"{s_d}T{s_t}"

    sorted_logs = sorted(logs, key=get_sort_key)
    latest_log = sorted_logs[-1] if sorted_logs else None

    # Auto-synchronize water_locations if it currently has an outdated status/sample_date
    if latest_log:
        try:
            loc = sb.table("water_locations").select("id, status, sample_date, sample_time").eq("id", location_id).single().execute()
            if loc.data:
                curr_date = loc.data.get("sample_date") or ""
                curr_time = loc.data.get("sample_time") or "00:00:00"
                curr_key = f"{curr_date}T{curr_time}"
                latest_key = get_sort_key(latest_log)

                # Sync only if the audit log is strictly newer than the station's sample date
                if latest_key > curr_key:
                    sync_payload = {
                        "status": latest_log.get("status") or loc.data.get("status"),
                        "sample_date": latest_log.get("sample_date"),
                        "sample_time": latest_log.get("sample_time"),
                        "bacteriological_exam": latest_log.get("bacteriological_exam"),
                        "coliform_bacteria": (latest_log.get("coliform_count") or 0) > 0 if latest_log.get("status") != "unknown" else False,
                        "e_coli": (latest_log.get("e_coli_count") or 0) > 0 if latest_log.get("status") != "unknown" else False,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }
                    if latest_log.get("notes"):
                        sync_payload["notes"] = latest_log["notes"]
                    sb.table("water_locations").update(sync_payload).eq("id", location_id).execute()
        except Exception as sync_err:
            logger.warning(f"Could not auto-sync water location status: {sync_err}")

    return {"success": True, "data": sorted_logs, "latest": latest_log}


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
        "full_name": full_name,
        "barangay": body.barangay,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "coliform_bacteria": None if status_val == "unknown" else (body.coliform_bacteria if body.coliform_bacteria is not None else ((body.coliform_count or 0) > 0)),
        "e_coli": None if status_val == "unknown" else (body.e_coli if body.e_coli is not None else ((body.e_coli_count or 0) > 0)),
        "bacteriological_exam": "untested" if status_val == "unknown" else exam,
        "image_url": body.image_url,
        "sample_date": body.sample_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sample_time": body.sample_time or datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "created_by": current_user["id"],
        "inspector_id": current_user["id"] if current_user["role"] == "sanitization_inspector" else None,
        "status": status_val,
        "notes": notes_val,
    }

    result = sb.table("water_locations").insert(new_loc).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create water location")

    # Record initial status in water_status_logs
    try:
        new_id = result.data[0]["id"]
        s_date = body.sample_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        s_time = body.sample_time or datetime.now(timezone.utc).strftime("%H:%M:%S")
        formatted_time = s_time if len(s_time) == 8 else f"{s_time}:00" if len(s_time) == 5 else "12:00:00"
        init_exam = body.bacteriological_exam
        if not init_exam:
            if status_val == "safe":
                init_exam = "passed"
            elif status_val == "unknown":
                init_exam = "untested"
            else:
                init_exam = "failed"

        sb.table("water_status_logs").insert({
            "location_id": new_id,
            "status": status_val,
            "previous_status": None,
            "bacteriological_exam": init_exam,
            "coliform_count": body.coliform_count if body.coliform_count is not None else (1 if body.coliform_bacteria else 0),
            "e_coli_count": body.e_coli_count if body.e_coli_count is not None else (1 if body.e_coli else 0),
            "notes": notes_val or ("Initial water source registration (Pending safety evaluation)" if status_val == "unknown" else "Initial water source registration"),
            "recorded_by": current_user["id"],
            "recorded_at": f"{s_date}T{formatted_time}+08:00",
        }).execute()
    except Exception as log_exc:
        logger.warning(f"Could not record initial water status log: {log_exc}")

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
        existing = sb.table("water_locations").select("*").eq("id", location_id).single().execute()
    except Exception as fetch_err:
        logger.warning(f"Could not fetch existing water location: {fetch_err}")

    if not existing or not existing.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    # If barangay official, ensure they can only edit their own barangay
    if current_user.get("role") == "barangay_official":
        user_brgy = (current_user.get("barangay") or "").strip().lower()
        existing_brgy = (existing.data.get("barangay") or "").strip().lower()
        if not user_brgy or existing_brgy != user_brgy:
            raise HTTPException(status_code=403, detail="You can only edit water stations in your assigned barangay.")
        body.barangay = existing.data.get("barangay")

    update_data = {k: v for k, v in body.model_dump().items() if v is not None}

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

    if "description" in update_data and not update_data.get("notes"):
        update_data["notes"] = update_data["description"]

    # Clean non-column fields if present
    update_data.pop("name", None)
    update_data.pop("description", None)
    update_data.pop("source_type", None)
    update_data.pop("type", None)
    update_data.pop("remarks", None)
    update_data.pop("coliform_count", None)
    update_data.pop("e_coli_count", None)

    # 1. Record status audit in water_status_logs
    old_status = existing.data.get("status") if existing and existing.data else None
    new_status = update_data.get("status") or old_status or "safe"
    s_date = update_data.get("sample_date") or (existing.data.get("sample_date") if existing and existing.data else None) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s_time = update_data.get("sample_time") or (existing.data.get("sample_time") if existing and existing.data else None) or datetime.now(timezone.utc).strftime("%H:%M:%S")

    formatted_time = s_time if len(s_time) == 8 else f"{s_time}:00" if len(s_time) == 5 else "12:00:00"
    rec_timestamp = f"{s_date}T{formatted_time}+08:00"

    # Replace any previous log for this exact date on this location so editing a day's status replaces it cleanly
    try:
        same_date_logs = sb.table("water_status_logs").select("id, recorded_at").eq("location_id", location_id).execute()
        for old_l in (same_date_logs.data or []):
            rec = old_l.get("recorded_at") or ""
            if rec.startswith(s_date) or (f"{s_date}T" in rec):
                sb.table("water_status_logs").delete().eq("id", old_l["id"]).execute()
    except Exception as del_err:
        logger.warning(f"Could not clean previous log for date {s_date}: {del_err}")

    try:
        log_entry = {
            "location_id": location_id,
            "status": new_status,
            "previous_status": old_status,
            "bacteriological_exam": "untested" if new_status == "unknown" else (update_data.get("bacteriological_exam") or (existing.data.get("bacteriological_exam") if existing and existing.data else None)),
            "coliform_count": 0 if new_status == "unknown" else (body.coliform_count if body.coliform_count is not None else (1 if update_data.get("coliform_bacteria") else 0)),
            "e_coli_count": 0 if new_status == "unknown" else (body.e_coli_count if body.e_coli_count is not None else (1 if update_data.get("e_coli") else 0)),
            "notes": update_data.get("notes") or f"Status updated to {new_status}",
            "recorded_by": current_user.get("id"),
            "recorded_at": rec_timestamp,
        }
        sb.table("water_status_logs").insert(log_entry).execute()
    except Exception as log_exc:
        logger.warning(f"Could not record water status log: {log_exc}")

    # 2. Determine chronologically latest audit test so water_locations always reflects the latest test
    try:
        all_logs_res = sb.table("water_status_logs").select("*").eq("location_id", location_id).order("recorded_at", desc=False).execute()
        all_logs = all_logs_res.data or []

        def get_log_sort_key(item):
            rec = item.get("recorded_at") or ""
            if "T" in rec:
                date_part = rec.split("T")[0]
                time_part = rec.split("T")[1].replace("Z", "").split("+")[0][:8]
                return f"{date_part}T{time_part}"
            return rec

        if all_logs:
            sorted_logs = sorted(all_logs, key=get_log_sort_key)
            latest_log = sorted_logs[-1]

            curr_sub_key = f"{s_date}T{formatted_time}"
            latest_key = get_log_sort_key(latest_log)

            # If user's submitted test is the latest (or >= latest key), use user's submitted status directly
            if curr_sub_key >= latest_key:
                effective_status = new_status
                effective_date = s_date
                effective_time = formatted_time
                effective_exam = "untested" if new_status == "unknown" else update_data.get("bacteriological_exam")
                effective_notes = update_data.get("notes")
            else:
                effective_status = latest_log.get("status") or new_status
                latest_rec = latest_log.get("recorded_at") or ""
                effective_date = latest_rec.split("T")[0] if "T" in latest_rec else s_date
                effective_time = latest_rec.split("T")[1].replace("Z", "").split("+")[0][:8] if "T" in latest_rec else formatted_time
                effective_exam = latest_log.get("bacteriological_exam")
                effective_notes = latest_log.get("notes")

            water_loc_sync = {
                "status": effective_status,
                "bacteriological_exam": "untested" if effective_status == "unknown" else effective_exam,
                "sample_date": effective_date,
                "sample_time": effective_time,
                "coliform_bacteria": ((latest_log.get("coliform_count") or 0) > 0) if effective_status != "unknown" else None,
                "e_coli": ((latest_log.get("e_coli_count") or 0) > 0) if effective_status != "unknown" else None,
            }
            if effective_notes:
                water_loc_sync["notes"] = effective_notes

            update_data.update(water_loc_sync)
    except Exception as latest_exc:
        logger.warning(f"Could not compute latest status log: {latest_exc}")

    if update_data.get("status") == "unknown":
        update_data["e_coli"] = None
        update_data["coliform_bacteria"] = None
        update_data["bacteriological_exam"] = "untested"

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("water_locations").update(update_data).eq("id", location_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    updated_station = result.data[0]

    # Automatically dispatch Boil Water advisory if water source transitioned to contaminated
    if new_status in ("contaminated", "undrinkable") and old_status not in ("contaminated", "undrinkable"):
        try:
            st_name = updated_station.get("full_name") or updated_station.get("name") or "Water Source"
            target_brgy = updated_station.get("barangay")
            auto_notif = {
                "title": f"⚠️ Boil Water Notice: {st_name}",
                "message": f"Water source '{st_name}' in Brgy. {target_brgy} has been flagged as CONTAMINATED (E. coli detected). Residents are strongly advised to boil water before drinking or use alternative safe stations.",
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
    """Delete a water location. Admin, Inspector, or Barangay Official."""
    sb = get_supabase()

    # Get location first to check permissions and delete image
    loc = sb.table("water_locations").select("image_url, barangay").eq("id", location_id).single().execute()
    if not loc.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    if current_user.get("role") == "barangay_official":
        user_brgy = (current_user.get("barangay") or "").strip().lower()
        existing_brgy = (loc.data.get("barangay") or "").strip().lower()
        if not user_brgy or existing_brgy != user_brgy:
            raise HTTPException(status_code=403, detail="You can only delete water stations in your assigned barangay.")

    if loc.data.get("image_url"):
        await delete_from_supabase(loc.data["image_url"])

    result = sb.table("water_locations").delete().eq("id", location_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "message": "Water location deleted"}


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
