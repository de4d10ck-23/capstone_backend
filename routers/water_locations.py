from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, UploadFile, File

from core.database import get_supabase
from core.dependencies import get_current_user, require_admin_or_inspector
from core.storage import upload_to_supabase, delete_from_supabase
from models.water_location import WaterLocationCreate, WaterLocationUpdate

router = APIRouter(prefix="/api/water-locations", tags=["Water Locations"])

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


@router.get("/")
async def list_water_locations(current_user: Annotated[dict, Depends(get_current_user)]):
    """Get all water locations. Barangay officials see only their barangay."""
    sb = get_supabase()
    query = sb.table("water_locations").select("*")

    # Barangay officials only see their barangay
    if current_user["role"] == "barangay_official" and current_user.get("barangay"):
        query = query.eq("barangay", current_user["barangay"])

    result = query.order("created_at", desc=True).execute()
    return {"success": True, "data": result.data or []}


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
        status = _compute_water_status(loc)
        loc["water_status"] = status
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


@router.post("/")
async def create_water_location(
    body: WaterLocationCreate,
    current_user: Annotated[dict, Depends(require_admin_or_inspector)],
):
    """Create a new water location. Admin or Inspector only."""
    sb = get_supabase()

    # Validate Maasin bounds
    if not (10.0 <= body.latitude <= 10.3) or not (124.7 <= body.longitude <= 125.1):
        raise HTTPException(status_code=400, detail="Coordinates must be within Maasin City bounds")

    # Normalize bacteriological_exam
    exam = (body.bacteriological_exam or "").strip().lower()
    if exam and exam not in ("passed", "failed", "untested"):
        exam = "untested"

    new_loc = {
        "full_name": body.full_name.strip(),
        "barangay": body.barangay,
        "latitude": body.latitude,
        "longitude": body.longitude,
        "coliform_bacteria": body.coliform_bacteria,
        "e_coli": body.e_coli,
        "bacteriological_exam": exam or "untested",
        "image_url": body.image_url,
        "sample_date": body.sample_date,
        "sample_time": body.sample_time,
        "created_by": current_user["id"],
        "inspector_id": current_user["id"] if current_user["role"] == "sanitization_inspector" else None,
        "status": "pending",
        "notes": body.notes,
    }

    result = sb.table("water_locations").insert(new_loc).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create water location")

    return {"success": True, "message": "Water location created", "data": result.data[0]}


@router.put("/{location_id}")
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

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    result = sb.table("water_locations").update(update_data).eq("id", location_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Water location not found")

    return {"success": True, "message": "Water location updated", "data": result.data[0]}


@router.delete("/{location_id}")
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


@router.post("/upload-image")
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
