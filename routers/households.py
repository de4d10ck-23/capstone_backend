from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from core.database import get_supabase
from core.dependencies import require_admin_cho_inspector

router = APIRouter(prefix="/api/households", tags=["Households"])


@router.get("", include_in_schema=False)
@router.get("/")
async def list_households(
    _user: Annotated[dict, Depends(require_admin_cho_inspector)],
):
    """Get household clusters for heatmap visualization."""
    sb = get_supabase()

    result = sb.rpc("get_household_clusters", {}).execute()

    # If RPC doesn't exist, fallback to direct query
    if not result.data:
        result = (
            sb.table("households")
            .select("longitude, latitude, toilet_facility, barangay_code")
            .not_.is_("longitude", "null")
            .not_.is_("latitude", "null")
            .gte("longitude", 124.7)
            .lte("longitude", 125.1)
            .gte("latitude", 10.0)
            .lte("latitude", 10.3)
            .limit(5000)
            .execute()
        )

    return {"success": True, "data": result.data or [], "total": len(result.data or [])}


@router.get("/risk-analysis")
async def get_risk_analysis(
    _user: Annotated[dict, Depends(require_admin_cho_inspector)],
    mode: Optional[str] = Query("qualitative"),
):
    """Get household risk analysis combining water quality and household density."""
    sb = get_supabase()

    mode = (mode or "qualitative").strip().lower()
    if mode not in ("qualitative", "quantitative"):
        mode = "qualitative"

    # Get contaminated water sources
    if mode == "quantitative":
        contaminated = (
            sb.table("water_locations")
            .select("longitude, latitude, coliform_bacteria, e_coli, full_name, bacteriological_exam")
            .eq("e_coli", True)
            .not_.is_("longitude", "null")
            .not_.is_("latitude", "null")
            .execute()
        )
    else:
        contaminated = (
            sb.table("water_locations")
            .select("longitude, latitude, coliform_bacteria, e_coli, full_name, bacteriological_exam")
            .eq("bacteriological_exam", "failed")
            .not_.is_("longitude", "null")
            .not_.is_("latitude", "null")
            .execute()
        )

    risk_zones = []
    for source in (contaminated.data or []):
        water_lng = float(source["longitude"])
        water_lat = float(source["latitude"])

        # Find nearby households
        nearby = (
            sb.table("households")
            .select("longitude, latitude")
            .gte("longitude", water_lng - 0.002)
            .lte("longitude", water_lng + 0.002)
            .gte("latitude", water_lat - 0.002)
            .lte("latitude", water_lat + 0.002)
            .execute()
        )

        household_count = len(nearby.data or [])
        if household_count > 0:
            risk_zones.append({
                "longitude": water_lng,
                "latitude": water_lat,
                "water_longitude": water_lng,
                "water_latitude": water_lat,
                "household_count": household_count,
                "risk_score": household_count * 2.0,
                "water_source": source["full_name"],
                "contamination_type": {
                    "coliform": bool(source.get("coliform_bacteria")),
                    "e_coli": bool(source.get("e_coli")),
                    "exam_failed": str(source.get("bacteriological_exam", "")).lower() == "failed",
                },
            })

    return {
        "success": True,
        "data": risk_zones,
        "contaminated_sources": len(contaminated.data or []),
        "mode": mode,
    }
