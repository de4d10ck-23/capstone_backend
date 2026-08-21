from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, get_optional_user, require_admin_or_inspector
from core.storage import upload_to_supabase, delete_from_supabase
from models.water_location import WaterLocationCreate, WaterLocationUpdate

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
        query = query.eq("barangay", current_user["barangay"])
    elif barangay:
        query = query.eq("barangay", barangay)

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
        .select("id, full_name, barangay, latitude, longitude, coliform_bacteria, e_coli, bacteriological_exam, sample_date")
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


@router.post("", include_in_schema=False)
@router.post("/")
async def create_water_location(
    body: WaterLocationCreate,
    current_user: Annotated[dict, Depends(require_admin_or_inspector)],
):
    """Create a new water location. Admin or Inspector only."""
    sb = get_supabase()

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
        "coliform_bacteria": body.coliform_bacteria if body.coliform_bacteria is not None else ((body.coliform_count or 0) > 0),
        "e_coli": body.e_coli if body.e_coli is not None else ((body.e_coli_count or 0) > 0),
        "bacteriological_exam": exam,
        "image_url": body.image_url,
        "sample_date": body.sample_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sample_time": body.sample_time or datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "created_by": current_user["id"],
        "inspector_id": current_user["id"] if current_user["role"] == "sanitization_inspector" else None,
        "status": status_val,
        "notes": notes_val,
    }

    try:
        result = sb.table("water_locations").insert(new_loc).execute()
    except Exception as e:
        # Fallback if image_url or some optional column doesn't exist
        cleaned = {k: v for k, v in new_loc.items() if v is not None}
        result = sb.table("water_locations").insert(cleaned).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create water location")

    return {"success": True, "message": "Water location created successfully", "data": result.data[0]}


@router.put("/{location_id}", include_in_schema=False)
@router.put("/{location_id}/")
async def update_water_location(
    location_id: str,
    body: WaterLocationUpdate,
    current_user: Annotated[dict, Depends(require_admin_or_inspector)],
):
    """Update a water location. Admin or Inspector only."""
    sb = get_supabase()

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

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("water_locations").update(update_data).eq("id", location_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "message": "Water location updated", "data": result.data[0]}


@router.delete("/{location_id}", include_in_schema=False)
@router.delete("/{location_id}/")
async def delete_water_location(
    location_id: str,
    current_user: Annotated[dict, Depends(require_admin_or_inspector)],
):
    """Delete a water location. Admin or Inspector only."""
    sb = get_supabase()

    # Get location first to delete image
    loc = sb.table("water_locations").select("image_url").eq("id", location_id).single().execute()
    if loc.data and loc.data.get("image_url"):
        await delete_from_supabase(loc.data["image_url"])

    result = sb.table("water_locations").delete().eq("id", location_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "message": "Water location deleted"}


@router.post("/upload-image", include_in_schema=False)
@router.post("/upload-image/")
async def upload_image(
    file: UploadFile = File(...),
    current_user: Annotated[dict, Depends(require_admin_or_inspector)] = None,
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
