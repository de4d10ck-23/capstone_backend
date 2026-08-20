from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query

from core.database import get_supabase
from core.dependencies import get_current_user

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])


def _normalize_mode(value: Optional[str]) -> str:
    mode = (value or "quantitative").strip().lower()
    return mode if mode in ("quantitative", "qualitative") else "quantitative"


@router.get("/overview")
async def get_overview(
    current_user: Annotated[dict, Depends(get_current_user)],
    mode: Optional[str] = Query("quantitative"),
):
    """Overview statistics for dashboard."""
    sb = get_supabase()
    mode = _normalize_mode(mode)

    # Get all water locations
    locations = sb.table("water_locations").select("*").execute()
    all_locs = locations.data or []

    total = len(all_locs)
    safe = warning = undrinkable = not_tested = 0

    for loc in all_locs:
        exam = (loc.get("bacteriological_exam") or "").strip().lower()
        coliform = loc.get("coliform_bacteria")
        e_coli = loc.get("e_coli")

        if mode == "qualitative":
            if exam == "passed":
                safe += 1
            elif exam == "failed":
                undrinkable += 1
            else:
                not_tested += 1
        else:
            if coliform is False and e_coli is False:
                safe += 1
            elif coliform is True and e_coli is False:
                warning += 1
            elif e_coli is True:
                undrinkable += 1
            elif coliform is None and e_coli is None:
                not_tested += 1

    # Household stats
    households_result = sb.table("households").select("toilet_facility").execute()
    all_households = households_result.data or []
    total_households = len(all_households)
    with_toilet = sum(1 for h in all_households if h.get("toilet_facility") == 1)

    # High risk households (near contaminated sources)
    if mode == "qualitative":
        contaminated = [l for l in all_locs if (l.get("bacteriological_exam") or "").lower() == "failed"]
    else:
        contaminated = [l for l in all_locs if l.get("e_coli") is True]

    high_risk_households = 0
    for source in contaminated:
        s_lat, s_lng = source.get("latitude"), source.get("longitude")
        if s_lat and s_lng:
            nearby = (
                sb.table("households")
                .select("id")
                .gte("latitude", s_lat - 0.002).lte("latitude", s_lat + 0.002)
                .gte("longitude", s_lng - 0.002).lte("longitude", s_lng + 0.002)
                .execute()
            )
            high_risk_households += len(nearby.data or [])

    return {
        "success": True,
        "data": {
            "mode": mode,
            "water_locations": {
                "total": total,
                "safe": safe,
                "warning": warning,
                "undrinkable": undrinkable,
                "not_tested": not_tested,
            },
            "households": {
                "total": total_households,
                "with_toilet": with_toilet,
                "without_toilet": total_households - with_toilet,
                "high_risk": high_risk_households,
                "medium_risk": 0,
            },
            "risk_assessment": {
                "high_risk_zones": len(contaminated),
                "affected_households": high_risk_households,
            },
        },
    }


@router.get("/barangay-stats")
async def get_barangay_stats(
    current_user: Annotated[dict, Depends(get_current_user)],
    mode: Optional[str] = Query("quantitative"),
):
    """Statistics grouped by barangay."""
    sb = get_supabase()
    mode = _normalize_mode(mode)

    locations = sb.table("water_locations").select("*").not_.is_("barangay", "null").execute()
    all_locs = locations.data or []

    # Group by barangay
    barangay_map: dict[str, list] = {}
    for loc in all_locs:
        brgy = loc.get("barangay", "")
        if brgy:
            barangay_map.setdefault(brgy, []).append(loc)

    result = []
    for name, locs in barangay_map.items():
        if mode == "qualitative":
            safe = sum(1 for l in locs if (l.get("bacteriological_exam") or "").lower() == "passed")
            undrinkable = sum(1 for l in locs if (l.get("bacteriological_exam") or "").lower() == "failed")
            not_tested = sum(1 for l in locs if (l.get("bacteriological_exam") or "").lower() in ("", "untested"))
            warning_count = 0
            contaminated = undrinkable
            risk_score = (undrinkable * 3) / max(len(locs), 1)
        else:
            safe = sum(1 for l in locs if l.get("coliform_bacteria") is False and l.get("e_coli") is False)
            warning_count = sum(1 for l in locs if l.get("coliform_bacteria") is True and l.get("e_coli") is False)
            undrinkable = sum(1 for l in locs if l.get("e_coli") is True)
            not_tested = sum(1 for l in locs if l.get("coliform_bacteria") is None and l.get("e_coli") is None)
            contaminated = sum(1 for l in locs if l.get("e_coli") is True or l.get("coliform_bacteria") is True)
            risk_score = (undrinkable * 3 + warning_count * 2) / max(len(locs), 1)

        result.append({
            "name": name,
            "total_locations": len(locs),
            "safe": safe,
            "warning": warning_count,
            "undrinkable": undrinkable,
            "not_tested": not_tested,
            "contaminated_sources": contaminated,
            "risk_score": risk_score,
        })

    result.sort(key=lambda x: x["risk_score"], reverse=True)
    return {"success": True, "data": result, "mode": mode}


@router.get("/water-quality-trends")
async def get_water_quality_trends(
    current_user: Annotated[dict, Depends(get_current_user)],
    mode: Optional[str] = Query("quantitative"),
):
    """Water quality trends over time."""
    sb = get_supabase()
    mode = _normalize_mode(mode)

    locations = (
        sb.table("water_locations")
        .select("*")
        .not_.is_("sample_date", "null")
        .order("sample_date")
        .execute()
    )

    trends: dict[str, dict] = {}
    for loc in (locations.data or []):
        date_str = loc.get("sample_date", "")
        if not date_str:
            continue
        if date_str not in trends:
            trends[date_str] = {"date": date_str, "safe": 0, "warning": 0, "undrinkable": 0, "not_tested": 0, "total": 0}

        trends[date_str]["total"] += 1
        exam = (loc.get("bacteriological_exam") or "").lower()

        if mode == "qualitative":
            if exam == "failed":
                trends[date_str]["undrinkable"] += 1
            elif exam == "passed":
                trends[date_str]["safe"] += 1
            else:
                trends[date_str]["not_tested"] += 1
        else:
            if loc.get("e_coli") is True:
                trends[date_str]["undrinkable"] += 1
            elif loc.get("coliform_bacteria") is True and loc.get("e_coli") is False:
                trends[date_str]["warning"] += 1
            elif loc.get("coliform_bacteria") is False and loc.get("e_coli") is False:
                trends[date_str]["safe"] += 1
            else:
                trends[date_str]["not_tested"] += 1

    return {"success": True, "data": list(trends.values()), "mode": mode}


@router.get("/contamination-heatmap")
async def get_contamination_heatmap(
    current_user: Annotated[dict, Depends(get_current_user)],
    mode: Optional[str] = Query("quantitative"),
):
    """Contamination heatmap data."""
    sb = get_supabase()
    mode = _normalize_mode(mode)

    if mode == "qualitative":
        contaminated = sb.table("water_locations").select("*").eq("bacteriological_exam", "failed").execute()
    else:
        contaminated = sb.table("water_locations").select("*").eq("e_coli", True).execute()

    sources_data = []
    for source in (contaminated.data or []):
        s_lat = source.get("latitude")
        s_lng = source.get("longitude")
        if not s_lat or not s_lng:
            continue

        nearby = (
            sb.table("households")
            .select("id")
            .gte("latitude", s_lat - 0.0045).lte("latitude", s_lat + 0.0045)
            .gte("longitude", s_lng - 0.0045).lte("longitude", s_lng + 0.0045)
            .execute()
        )

        sources_data.append({
            "id": source["id"],
            "name": source["full_name"],
            "latitude": s_lat,
            "longitude": s_lng,
            "mode": mode,
            "type": "failed_exam" if mode == "qualitative" else "e_coli",
            "severity": 2,
            "affected_households": len(nearby.data or []),
            "barangay": source.get("barangay"),
        })

    return {"success": True, "data": sources_data, "mode": mode}


@router.get("/household-coverage")
async def get_household_coverage(
    current_user: Annotated[dict, Depends(get_current_user)],
):
    """Household toilet facility coverage statistics."""
    sb = get_supabase()

    households = sb.table("households").select("barangay_code, toilet_facility").execute()
    all_h = households.data or []

    # Group by barangay
    brgy_map: dict[str, list] = {}
    for h in all_h:
        code = h.get("barangay_code", "")
        if code:
            brgy_map.setdefault(code, []).append(h)

    coverage = []
    for code, hlist in brgy_map.items():
        total = len(hlist)
        with_toilet = sum(1 for h in hlist if h.get("toilet_facility") == 1)
        coverage.append({
            "barangay": code,
            "total_households": total,
            "with_toilet": with_toilet,
            "without_toilet": total - with_toilet,
            "coverage_percentage": round((with_toilet / total * 100) if total > 0 else 0, 2),
        })

    coverage.sort(key=lambda x: x["coverage_percentage"])
    return {"success": True, "data": coverage}
