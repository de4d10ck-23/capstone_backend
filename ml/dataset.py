import os
import sys
import json
import random
import numpy as np
import pandas as pd
from typing import Tuple, List, Dict, Any

# Ensure backend root is on sys.path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_ROOT = os.path.dirname(CURRENT_DIR)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

import shapely.geometry
from shapely.geometry import shape, Point

from core.database import get_supabase

# ============================================================================
# RECOMMENDED FEATURE PARAMETERS FOR WATER CONTAMINATION FORECASTING
# Training compatibility schema. Spatial lookup now uses the new study map.
# Production uses a neutral-hazard RF baseline plus explicit hazard adjustments;
# historical RF validation metrics do not validate that combined risk index.
# ============================================================================
FEATURE_COLUMNS = [
    "flood_susceptibility_level",      # Legacy slot: new F encodes presence as 1, not moderate severity
    "landslide_susceptibility_level",  # 0: None/Low, 1: Moderate (ML), 2: High (HL)
    "rainfall_intensity_mm",           # 24-hour cumulative rainfall (mm)
    "cumulative_48h_rainfall_mm",      # 48-hour cumulative rainfall (mm)
    "temperature_celsius",             # Ambient temperature (°C)
    "humidity_percent",                # Relative humidity (%)
    "source_type_code",                # 0: Open Spring, 1: Reservoir/Tank, 2: Well/Pump, 3: Tap/Faucet
]

SOURCE_TYPE_MAPPING = {
    "open spring": 0,
    "spring": 0,
    "reservoir": 1,
    "tank": 1,
    "water tank": 1,
    "pump": 2,
    "jetmatic": 2,
    "deep well": 2,
    "shallow well": 2,
    "well": 2,
    "faucet": 3,
    "tap": 3,
    "bis": 3,
    "level 3": 3,
    "communal faucet": 3,
}

DATA_DIR = os.path.join(BACKEND_ROOT, "data")
_cached_hazard_shapes = None


def load_hazard_shapes():
    """Compatibility accessor backed exclusively by the new study map."""
    from services.hazard_risk_service import load_study_data
    features = load_study_data()['features']
    return tuple([(shape(f['geometry']), f['properties']) for f in features
                  if f['properties']['kind'] == kind] for kind in ('flood', 'landslide'))


def get_station_hazard_levels(lat: float, lng: float) -> Tuple[int, int]:
    """Legacy numeric encoding from new zones; F means susceptible, not high."""
    from services.hazard_risk_service import assess_station_hazards
    assessment = assess_station_hazards(lat, lng, '', [])
    return assessment['flood_level'], assessment['landslide_level']


def encode_source_type(source_type_str: str, name_str: str = "") -> int:
    """Encode water source sanitary protection level into integer code (0 to 3)."""
    combined = f"{source_type_str} {name_str}".lower()
    for key, code in SOURCE_TYPE_MAPPING.items():
        if key in combined:
            return code
    return 1  # Default to 1 (Protected Reservoir/Tank)


def fetch_raw_data() -> List[Dict[str, Any]]:
    """Fetch all physical water monitoring stations from Supabase."""
    client = get_supabase()
    locs_res = client.table("water_locations").select("*").is_("archived_at", "null").execute()
    return locs_res.data or []


def build_base_records(locations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract spatial hazard parameters and baseline bacteriological test labels."""
    records = []

    for loc in locations:
        lat = loc.get("latitude")
        lng = loc.get("longitude")
        if lat is None or lng is None:
            continue

        try:
            lat = float(lat)
            lng = float(lng)
        except (ValueError, TypeError):
            continue

        flood_lvl, landslide_lvl = get_station_hazard_levels(lat, lng)

        raw_status = (loc.get("status") or "safe").lower()
        raw_ecoli = loc.get("e_coli")
        coliform = float(loc.get("coliform_bacteria") or loc.get("coliform_count") or 0)
        has_ecoli = (
            raw_ecoli is True
            or str(raw_ecoli).lower() in ["positive", "present", "true", "1"]
            or float(loc.get("e_coli_count") or 0) > 0
        )
        is_unsafe = (
            raw_status in ["undrinkable", "contaminated", "warning"]
            or has_ecoli
            or coliform > 0
        )
        label = 1 if is_unsafe else 0

        station_name = loc.get("full_name") or loc.get("name") or "Water Station"
        source_code = encode_source_type(loc.get("source_type") or "", station_name)

        records.append({
            "id": loc.get("id"),
            "name": station_name,
            "barangay": loc.get("barangay"),
            "latitude": lat,
            "longitude": lng,
            "source_type_code": source_code,
            "flood_susceptibility_level": flood_lvl,
            "landslide_susceptibility_level": landslide_lvl,
            "baseline_label": label,
        })

    return records


def generate_scenario_dataset(
    base_records: List[Dict[str, Any]], random_seed: int = 42
) -> pd.DataFrame:
    """
    Augment baseline stations across 3 meteorological and runoff regimes:
      1. Dry / Base Period (0 - 5 mm rainfall)
      2. Moderate Monsoon Runoff (15 - 45 mm rainfall)
      3. Severe Storm Inundation (60 - 140 mm rainfall)
    """
    random.seed(random_seed)
    np.random.seed(random_seed)

    rows = []

    for station in base_records:
        src_code = station["source_type_code"]
        flood_lvl = station["flood_susceptibility_level"]
        landslide_lvl = station["landslide_susceptibility_level"]
        base_label = station["baseline_label"]

        # Scenario 1: Dry / Normal Baseline Weather
        for _ in range(2):
            rf_24h = round(float(np.random.uniform(0.0, 5.0)), 1)
            rf_48h = round(rf_24h + float(np.random.uniform(0.0, 8.0)), 1)
            temp = round(float(np.random.uniform(28.0, 33.0)), 1)
            humidity = round(float(np.random.uniform(65.0, 78.0)), 1)

            # In dry weather, baseline bacteriological ground truth holds
            label = base_label
            # Unprotected open spring in high hazard zone remains contaminated
            if src_code == 0 and flood_lvl >= 1 and base_label == 1:
                label = 1

            rows.append({
                "flood_susceptibility_level": flood_lvl,
                "landslide_susceptibility_level": landslide_lvl,
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

            # Rain triggers runoff:
            # High flood zones (HF=2), high landslide zones (HL=2), and open springs (src=0) contaminate
            elevated = False
            if base_label == 1:
                elevated = True
            elif flood_lvl == 2 or landslide_lvl == 2:
                elevated = True
            elif src_code == 0 and (flood_lvl >= 1 or rf_48h > 45.0):
                elevated = True
            elif src_code == 2 and flood_lvl >= 1 and rf_24h > 35.0:  # shallow well
                elevated = True

            label = 1 if elevated else 0

            rows.append({
                "flood_susceptibility_level": flood_lvl,
                "landslide_susceptibility_level": landslide_lvl,
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

        # Scenario 3: Severe Tropical Storm / Typhoon Inundation (60 - 150 mm)
        for _ in range(2):
            rf_24h = round(float(np.random.uniform(60.0, 130.0)), 1)
            rf_48h = round(rf_24h + float(np.random.uniform(40.0, 90.0)), 1)
            temp = round(float(np.random.uniform(23.0, 26.5)), 1)
            humidity = round(float(np.random.uniform(92.0, 99.0)), 1)

            # High floodwaters contaminate most vulnerable water infrastructure
            elevated = True
            # Only sealed Level 3 taps and elevated deep wells outside hazard zones remain protected
            if src_code == 3 and flood_lvl == 0 and landslide_lvl == 0:
                elevated = False
            elif src_code == 1 and flood_lvl == 0 and landslide_lvl == 0 and rf_24h < 85.0:
                elevated = False

            label = 1 if elevated else 0

            rows.append({
                "flood_susceptibility_level": flood_lvl,
                "landslide_susceptibility_level": landslide_lvl,
                "rainfall_intensity_mm": rf_24h,
                "cumulative_48h_rainfall_mm": rf_48h,
                "temperature_celsius": temp,
                "humidity_percent": humidity,
                "source_type_code": src_code,
                "target_unsafe": label,
                "scenario": "Severe Storm Inundation",
                "station_name": station["name"],
                "barangay": station["barangay"],
            })

    df = pd.DataFrame(rows)
    return df


def get_training_data() -> Tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """
    Main loader function: fetches data, calculates spatial hazard metrics, generates augmented dataset.
    Returns: (df, X, y)
    """
    locations = fetch_raw_data()
    print(f"Loaded {len(locations)} stations from Supabase.")

    base_records = build_base_records(locations)
    df = generate_scenario_dataset(base_records)

    X = df[FEATURE_COLUMNS].values
    y = df["target_unsafe"].values

    print(f"Constructed augmented dataset with {len(df)} rows using {len(FEATURE_COLUMNS)} features:")
    for col in FEATURE_COLUMNS:
        print(f"  • {col}")
    print(f"Class distribution: Safe (0) = {(y == 0).sum()}, Unsafe (1) = {(y == 1).sum()}")

    return df, X, y


if __name__ == "__main__":
    df, X, y = get_training_data()
    output_path = os.path.join(CURRENT_DIR, "water_quality_dataset.csv")
    df.to_csv(output_path, index=False)
    print(f"Saved dataset snapshot to: {output_path}")
