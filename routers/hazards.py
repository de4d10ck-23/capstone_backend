import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.database import get_supabase
from core.dependencies import (
    get_optional_user,
    require_admin_or_inspector,
    require_staff,
)
from core.spatial import calculate_station_hazard_distances
from models.hazard import HazardCreate, HazardUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hazards", tags=["Contamination Hazards"])


@router.get("/proximity")
async def get_hazard_proximity(
    lat: float = Query(...),
    lng: float = Query(...),
):
    """
    Calculate spatial distances (in meters) from a coordinate (lat, lng)
    to the nearest mapped latrine/septic tank, river/stream, and agricultural land.
    """
    sb = get_supabase()
    try:
        res = sb.table("contamination_hazards").select("*").execute()
        hazards = res.data or []
    except Exception as exc:
        logger.warning(f"Error reading hazards for proximity: {exc}")
        hazards = []

    distances = calculate_station_hazard_distances((lng, lat), hazards)
    return {"success": True, "coordinates": [lng, lat], "distances": distances}


@router.get("")
@router.get("/")
async def list_hazards(
    barangay: Optional[str] = Query(None),
    hazard_type: Optional[str] = Query(None),
    _user: Annotated[dict | None, Depends(get_optional_user)] = None,
):
    """
    List all mapped contamination hazards (points, polylines, polygons).
    Publicly accessible so residents, staff, and ML pipelines can query hazards.
    Also returns a GeoJSON FeatureCollection for direct consumption by Mapbox GL.
    """
    sb = get_supabase()
    try:
        query = sb.table("contamination_hazards").select("*").order("created_at", desc=True)
        if barangay:
            query = query.ilike("barangay", f"%{barangay.strip()}%")
        if hazard_type:
            query = query.eq("hazard_type", hazard_type.strip())

        res = query.execute()
        rows = res.data or []
    except Exception as exc:
        logger.warning(f"Error reading contamination_hazards table: {exc}")
        # Return empty list gracefully if table is not yet migrated in Supabase
        rows = []

    # Format as GeoJSON FeatureCollection
    features = []
    for r in rows:
        geom_type = r.get("geometry_type") or "Point"
        coords = r.get("coordinates")
        if coords is not None:
            features.append({
                "type": "Feature",
                "id": r.get("id"),
                "geometry": {
                    "type": geom_type,
                    "coordinates": coords,
                },
                "properties": {
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "hazard_type": r.get("hazard_type"),
                    "risk_level": r.get("risk_level", "high"),
                    "barangay": r.get("barangay"),
                    "notes": r.get("notes"),
                    "created_at": r.get("created_at"),
                }
            })

    geojson = {
        "type": "FeatureCollection",
        "features": features,
    }

    return {
        "success": True,
        "total": len(rows),
        "data": rows,
        "geojson": geojson,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_hazard(
    body: HazardCreate,
    current_user: Annotated[dict, Depends(require_staff)],
):
    """
    Create a new contamination hazard (Point, LineString, or Polygon).
    Authorized for Sanitization Inspectors, Admins, CHO, and Barangay Officials.
    """
    sb = get_supabase()

    record = {
        "name": body.name.strip(),
        "hazard_type": body.hazard_type.strip(),
        "geometry_type": body.geometry_type.strip(),
        "coordinates": body.coordinates,
        "barangay": body.barangay.strip() if body.barangay else None,
        "risk_level": body.risk_level or "high",
        "notes": body.notes.strip() if body.notes else None,
        "created_by": current_user.get("id"),
    }

    try:
        res = sb.table("contamination_hazards").insert(record).execute()
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save hazard. Please ensure the schema migration has been applied.",
            )
        return {"success": True, "data": res.data[0]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to insert hazard: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while saving hazard: {str(exc)}",
        )


@router.put("/{hazard_id}")
async def update_hazard(
    hazard_id: str,
    body: HazardUpdate,
    current_user: Annotated[dict, Depends(require_staff)],
):
    """Update hazard metadata (name, type, risk level, notes)."""
    sb = get_supabase()
    updates = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.hazard_type is not None:
        updates["hazard_type"] = body.hazard_type.strip()
    if body.risk_level is not None:
        updates["risk_level"] = body.risk_level.strip()
    if body.notes is not None:
        updates["notes"] = body.notes.strip()

    if not updates:
        return {"success": True, "message": "Nothing to update"}

    try:
        res = sb.table("contamination_hazards").update(updates).eq("id", hazard_id).execute()
        if not res.data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hazard not found")
        return {"success": True, "data": res.data[0]}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while updating hazard: {str(exc)}",
        )


@router.delete("/{hazard_id}")
async def delete_hazard(
    hazard_id: str,
    current_user: Annotated[dict, Depends(require_staff)],
):
    """Delete a contamination hazard."""
    sb = get_supabase()
    try:
        res = sb.table("contamination_hazards").delete().eq("id", hazard_id).execute()
        return {"success": True, "message": f"Hazard {hazard_id} deleted successfully"}
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database error while deleting hazard: {str(exc)}",
        )
