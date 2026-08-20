from pydantic import BaseModel
from typing import Optional


class ForecastConfig(BaseModel):
    """ML model configuration parameters (stored in a single-row config table)."""
    rainfall_weight: Optional[float] = 0.3
    temperature_weight: Optional[float] = 0.2
    humidity_weight: Optional[float] = 0.15
    proximity_weight: Optional[float] = 0.2
    historical_weight: Optional[float] = 0.15
    risk_threshold_low: Optional[float] = 0.3
    risk_threshold_high: Optional[float] = 0.7
    prediction_days: Optional[int] = 7
