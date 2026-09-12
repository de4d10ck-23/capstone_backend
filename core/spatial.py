import math
from typing import List, Tuple, Dict, Any

EARTH_RADIUS_METERS = 6371000.0


def haversine_distance(coord1: Tuple[float, float], coord2: Tuple[float, float]) -> float:
    """
    Calculate great-circle distance between two points (lng, lat) in meters.
    coord1: (lng, lat)
    coord2: (lng, lat)
    """
    lng1, lat1 = coord1
    lng2, lat2 = coord2

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_METERS * c


def _project_point_to_segment(
    p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]
) -> float:
    """Approximate distance in meters from point p to line segment a-b."""
    # Convert degrees to planar meters approx around p
    lat_mid = (a[1] + b[1]) / 2.0
    meters_per_deg_lat = 111132.92
    meters_per_deg_lng = 111412.84 * math.cos(math.radians(lat_mid))

    px = p[0] * meters_per_deg_lng
    py = p[1] * meters_per_deg_lat
    ax = a[0] * meters_per_deg_lng
    ay = a[1] * meters_per_deg_lat
    bx = b[0] * meters_per_deg_lng
    by = b[1] * meters_per_deg_lat

    dx = bx - ax
    dy = by - ay
    l2 = dx * dx + dy * dy

    if l2 == 0:
        return math.hypot(px - ax, py - ay)

    # Consider the line extending the segment, parameterized as a + t (b - a).
    # We find projection of point p onto the line.
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    proj_x = ax + t * dx
    proj_y = ay + t * dy

    return math.hypot(px - proj_x, py - proj_y)


def distance_point_to_linestring(point: Tuple[float, float], line_coords: List[List[float]]) -> float:
    """Distance in meters from point (lng, lat) to a LineString [[lng, lat], ...]."""
    if not line_coords or len(line_coords) == 0:
        return float("inf")
    if len(line_coords) == 1:
        return haversine_distance(point, (line_coords[0][0], line_coords[0][1]))

    min_dist = float("inf")
    for i in range(len(line_coords) - 1):
        a = (line_coords[i][0], line_coords[i][1])
        b = (line_coords[i + 1][0], line_coords[i + 1][1])
        dist = _project_point_to_segment(point, a, b)
        if dist < min_dist:
            min_dist = dist
    return min_dist


def _point_in_polygon(point: Tuple[float, float], ring: List[List[float]]) -> bool:
    """Ray casting algorithm to determine if point is strictly inside polygon ring."""
    x, y = point[0], point[1]
    inside = False
    n = len(ring)
    for i in range(n):
        j = (i - 1) % n
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersect = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi)
        if intersect:
            inside = not inside
    return inside


def distance_point_to_polygon(point: Tuple[float, float], poly_coords: Any) -> float:
    """
    Distance in meters from point (lng, lat) to a Polygon.
    poly_coords: [[[lng, lat], ...]] (GeoJSON exterior ring and optional interior rings).
    If point is inside the polygon, returns 0.0 meters.
    """
    if not poly_coords or len(poly_coords) == 0:
        return float("inf")

    exterior_ring = poly_coords[0] if isinstance(poly_coords[0][0], (list, tuple)) else poly_coords

    # Check if inside
    if _point_in_polygon(point, exterior_ring):
        return 0.0

    # Otherwise distance to exterior boundary
    return distance_point_to_linestring(point, exterior_ring)


def calculate_station_hazard_distances(
    station_coord: Tuple[float, float], hazards: List[Dict[str, Any]]
) -> Dict[str, float]:
    """
    Computes spatial distances (in meters) from a water station coordinate (lng, lat)
    to the nearest mapped hazards grouped by hazard type:
      - min_dist_latrine_meters
      - min_dist_river_meters
      - min_dist_farmland_meters
      - min_dist_any_hazard_meters
    """
    min_dist_latrine = 5000.0  # Default cap (5km) if none found
    min_dist_river = 5000.0
    min_dist_farmland = 5000.0
    min_dist_any = 5000.0

    for h in hazards:
        geom_type = h.get("geometry_type") or "Point"
        h_type = (h.get("hazard_type") or "").lower()
        coords = h.get("coordinates")
        if not coords:
            continue

        dist = float("inf")
        if geom_type == "Point":
            dist = haversine_distance(station_coord, (coords[0], coords[1]))
        elif geom_type == "LineString":
            dist = distance_point_to_linestring(station_coord, coords)
        elif geom_type == "Polygon":
            dist = distance_point_to_polygon(station_coord, coords)

        if dist < min_dist_any:
            min_dist_any = dist

        # Categorize
        if any(k in h_type for k in ["latrine", "septic", "toilet", "piggery"]):
            if dist < min_dist_latrine:
                min_dist_latrine = dist
        elif any(k in h_type for k in ["river", "stream", "drainage", "waterway"]):
            if dist < min_dist_river:
                min_dist_river = dist
        elif any(k in h_type for k in ["agri", "farm", "rice", "field"]):
            if dist < min_dist_farmland:
                min_dist_farmland = dist

    return {
        "min_dist_latrine_meters": round(min_dist_latrine, 1),
        "min_dist_river_meters": round(min_dist_river, 1),
        "min_dist_farmland_meters": round(min_dist_farmland, 1),
        "min_dist_any_hazard_meters": round(min_dist_any, 1),
    }
