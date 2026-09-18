import os
import sys
import random
import numpy as np
import pandas as pd
from typing import Tuple, List, Dict, Any

# Ensure backend root is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

from core.database import get_supabase
from core.spatial import (
    calculate_station_hazard_distances,
    haversine_distance,
    distance_point_to_linestring,
    distance_point_to_polygon,
)

FEATURE_COLUMNS = [
    "distance_to_latrine_meters",
    "distance_to_river_meters",
    "distance_to_farmland_meters",
    "buffer_encroachments_count",
    "rainfall_intensity_mm",
    "cumulative_48h_rainfall_mm",
    "temperature_celsius",
    "humidity_percent",
    "source_type_code",
]

SOURCE_TYPE_MAPPING = {
    "open spring": 0,
    "spring": 0,
    "reservoir": 1,
    "tank": 1,
    "pump": 2,
    "jetmatic": 2,
    "deep well": 2,
    "well": 2,
    "faucet": 3,
    "tap": 3,
    "bis": 3,
}


def encode_source_type(source_type_str: str, name_str: str = "") -> int:
    combined = f"{source_type_str} {name_str}".lower()
    for key, code in SOURCE_TYPE_MAPPING.items():
        if key in combined:
            return code
    return 1  # Default to 1 (Protected Reservoir/Tank)


def count_buffer_encroachments(
    station_coord: Tuple[float, float], hazards: List[Dict[str, Any]], buffer_meters: float = 50.0
) -> int:
    """Count how many hazards fall within buffer_meters of the station."""
    count = 0
    for h in hazards:
        geom_type = h.get("geometry_type") or "Point"
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

        if dist <= buffer_meters:
            count += 1
    return count


def fetch_raw_data() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch water stations and contamination hazards from Supabase."""
    client = get_supabase()
    locs_res = client.table("water_locations").select("*").execute()
    hazards_res = client.table("contamination_hazards").select("*").execute()

    return locs_res.data or [], hazards_res.data or []


def build_base_records(
    locations: List[Dict[str, Any]], hazards: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert DB records to station feature objects with GIS distance calculations."""
    records = []

    for loc in locations:
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            continue

        coord = (float(lng), float(lat))
        dist_dict = calculate_station_hazard_distances(coord, hazards)
        encroachments = count_buffer_encroachments(coord, hazards, 50.0)

        # Baseline ground-truth label from database
        raw_status = (loc.get("status") or "safe").lower()
        raw_ecoli = loc.get("e_coli")
        is_unsafe = (
            raw_status in ["undrinkable", "contaminated", "warning"]
            or raw_ecoli is True
            or str(raw_ecoli).lower() in ["positive", "present", "true", "1"]
        )
        label = 1 if is_unsafe else 0

        station_name = loc.get("full_name") or loc.get("name") or ""
        source_code = encode_source_type(
            loc.get("source_type") or "", station_name
        )

        records.append({
            "id": loc.get("id"),
            "name": station_name,
            "barangay": loc.get("barangay"),
            "latitude": lat,
            "longitude": lng,
            "source_type_code": source_code,
            "distance_to_latrine_meters": dist_dict["min_dist_latrine_meters"],
            "distance_to_river_meters": dist_dict["min_dist_river_meters"],
            "distance_to_farmland_meters": dist_dict["min_dist_farmland_meters"],
            "buffer_encroachments_count": encroachments,
            "baseline_label": label,
        })

    return records


def generate_scenario_dataset(
    base_records: List[Dict[str, Any]], random_seed: int = 42
) -> pd.DataFrame:
    """
    Augment baseline stations across 3 meteorological scenarios:
      1. Dry / Base period (0 - 5 mm rainfall)
      2. Moderate Monsoon (15 - 45 mm rainfall)
      3. Severe Storm Runoff (60 - 140 mm rainfall)
    Applies non-linear domain environmental mechanics:
      - Saturated soil + close latrine/river significantly elevates microbial transport.
      - Deep protected pumps & BIS taps resist runoff better than open springs.
    """
    random.seed(random_seed)
    np.random.seed(random_seed)

    rows = []

    for station in base_records:
        src_code = station["source_type_code"]
        dist_latrine = station["distance_to_latrine_meters"]
        dist_river = station["distance_to_river_meters"]
        dist_farm = station["distance_to_farmland_meters"]
        encroachments = station["buffer_encroachments_count"]
        base_label = station["baseline_label"]

        # Scenario 1: Dry / Normal (Similar to ground-truth testing conditions in Leyte dry window)
        for _ in range(2):
            rf_24h = round(float(np.random.uniform(0.0, 5.0)), 1)
            rf_48h = round(rf_24h + float(np.random.uniform(0.0, 8.0)), 1)
            temp = round(float(np.random.uniform(28.0, 33.0)), 1)
            humidity = round(float(np.random.uniform(65.0, 78.0)), 1)

            # In dry periods, label tracks ground truth baseline closely
            label = base_label
            # Open spring with latrine < 30m remains unsafe even in dry weather
            if src_code == 0 and dist_latrine < 35:
                label = 1

            rows.append({
                "distance_to_latrine_meters": dist_latrine,
                "distance_to_river_meters": dist_river,
                "distance_to_farmland_meters": dist_farm,
                "buffer_encroachments_count": encroachments,
                "rainfall_intensity_mm": rf_24h,
                "cumulative_48h_rainfall_mm": rf_48h,
                "temperature_celsius": temp,
                "humidity_percent": humidity,
                "source_type_code": src_code,
                "target_unsafe": label,
                "scenario": "Dry / Normal",
                "station_name": station["name"],
                "barangay": station["barangay"],
            })

        # Scenario 2: Moderate Monsoon Shower (15 - 45 mm)
        for _ in range(2):
            rf_24h = round(float(np.random.uniform(15.0, 45.0)), 1)
            rf_48h = round(rf_24h + float(np.random.uniform(10.0, 30.0)), 1)
            temp = round(float(np.random.uniform(25.0, 28.5)), 1)
            humidity = round(float(np.random.uniform(80.0, 92.0)), 1)

            # Moderate rain causes runoff:
            # Latrine within 75m or River within 50m elevates microbial risk for springs & wells
            elevated = False
            if base_label == 1:
                elevated = True
            elif dist_latrine < 80 or dist_river < 60 or encroachments >= 1:
                elevated = True
            elif src_code == 0 and (dist_farm < 100 or rf_48h > 50):  # open spring
                elevated = True

            label = 1 if elevated else 0

            rows.append({
                "distance_to_latrine_meters": dist_latrine,
                "distance_to_river_meters": dist_river,
                "distance_to_farmland_meters": dist_farm,
                "buffer_encroachments_count": encroachments,
                "rainfall_intensity_mm": rf_24h,
                "cumulative_48h_rainfall_mm": rf_48h,
                "temperature_celsius": temp,
                "humidity_percent": humidity,
                "source_type_code": src_code,
                "target_unsafe": label,
                "scenario": "Moderate Monsoon",
                "station_name": station["name"],
                "barangay": station["barangay"],
            })

        # Scenario 3: Severe Tropical Storm / Typhoon Runoff (60 - 150 mm)
        for _ in range(2):
            rf_24h = round(float(np.random.uniform(60.0, 130.0)), 1)
            rf_48h = round(rf_24h + float(np.random.uniform(40.0, 90.0)), 1)
            temp = round(float(np.random.uniform(23.0, 26.5)), 1)
            humidity = round(float(np.random.uniform(92.0, 99.0)), 1)

            # High storm runoff:
            # Most shallow/open systems and stations within 150m of latrines/rivers contaminate
            elevated = True
            # Only highly protected BIS taps and deep wells far from hazards (> 250m) stay safe
            if src_code == 3 and dist_latrine > 200 and dist_river > 150 and encroachments == 0:
                elevated = False
            elif src_code == 2 and dist_latrine > 250 and dist_river > 200:
                elevated = False

            label = 1 if elevated else 0

            rows.append({
                "distance_to_latrine_meters": dist_latrine,
                "distance_to_river_meters": dist_river,
                "distance_to_farmland_meters": dist_farm,
                "buffer_encroachments_count": encroachments,
                "rainfall_intensity_mm": rf_24h,
                "cumulative_48h_rainfall_mm": rf_48h,
                "temperature_celsius": temp,
                "humidity_percent": humidity,
                "source_type_code": src_code,
                "target_unsafe": label,
                "scenario": "Severe Storm Runoff",
                "station_name": station["name"],
                "barangay": station["barangay"],
            })

    df = pd.DataFrame(rows)
    return df


def get_training_data() -> Tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """
    Main loader function: fetches data, calculates spatial metrics, generates augmented dataset.
    Returns: (df, X, y)
    """
    locations, hazards = fetch_raw_data()
    print(f"Loaded {len(locations)} stations and {len(hazards)} mapped hazards from Supabase.")

    base_records = build_base_records(locations, hazards)
    df = generate_scenario_dataset(base_records)

    X = df[FEATURE_COLUMNS].values
    y = df["target_unsafe"].values

    print(f"Constructed augmented dataset with {len(df)} rows.")
    print(f"Class distribution: Safe (0) = {(y == 0).sum()}, Unsafe (1) = {(y == 1).sum()}")

    return df, X, y


if __name__ == "__main__":
    df, X, y = get_training_data()
    output_path = os.path.join(CURRENT_DIR, "augmented_water_dataset.csv")
    df.to_csv(output_path, index=False)
    print(f"Saved dataset snapshot to: {output_path}")
