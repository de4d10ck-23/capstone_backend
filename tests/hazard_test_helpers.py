"""Pure hazard dependencies for isolated forecasts; no credentials or network."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.hazard_risk_service import assess_station_hazards, apply_hazard_adjustment, HAZARD_MODEL_VERSION


def hazard_dependencies():
    return dict(assess_station_hazards=lambda lat, lng, area, records:
                assess_station_hazards(lat, lng, area, records, features=[]),
                apply_hazard_adjustment=apply_hazard_adjustment,
                fetch_hazard_records=lambda: [], HAZARD_MODEL_VERSION=HAZARD_MODEL_VERSION)
