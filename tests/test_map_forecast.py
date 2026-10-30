"""Forecast date boundaries, actual weather inputs, and unavailable-data behavior."""
import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_push_delivery import load_functions
from hazard_test_helpers import hazard_dependencies


class ForecastWeatherTests(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(timezone(timedelta(hours=8))).date()
        self.days = [(self.today + timedelta(days=i)).isoformat() for i in range(-1, 8)]
        self.daily = {"time": self.days, "precipitation_sum": list(range(9)),
                      "temperature_2m_mean": [27.0] * 9, "relative_humidity_2m_mean": [82.0] * 9}
        self.response = Mock()
        self.response.json.return_value = {"daily": self.daily}
        self.client = AsyncMock()
        self.client.get.return_value = self.response
        self.context = AsyncMock()
        self.context.__aenter__.return_value = self.client
        self.scope = load_functions("services/weather_service.py", {"get_forecast_weather"},
            timedelta=timedelta, timezone=timezone, time=time, _FORECAST_CACHE={}, _FORECAST_ERRORS={},
            CACHE_TTL_SECONDS=600, httpx=SimpleNamespace(AsyncClient=lambda **_: self.context))

    def fetch(self, offset):
        return asyncio.run(self.scope["get_forecast_weather"](
            (self.today + timedelta(days=offset)).isoformat(), 10.13, 124.87))

    def test_today_and_seven_days_ahead_use_correct_daily_weather(self):
        for offset in range(8):
            with self.subTest(offset=offset):
                result = self.fetch(offset)
                self.assertEqual(result["rainfall_24h_mm"], offset + 1)
                self.assertEqual(result["rainfall_48h_mm"], 2 * offset + 1)
        today = self.fetch(0)
        last = self.fetch(7)
        self.assertEqual(today["rainfall_24h_mm"], 1)
        self.assertEqual(last["rainfall_24h_mm"], 8)
        self.assertEqual(last["rainfall_48h_mm"], 15)
        self.assertEqual(last["humidity_percent"], 82)
        self.assertEqual(self.client.get.await_count, 1)
        params = self.client.get.call_args.kwargs["params"]
        self.assertEqual(params["forecast_days"], 8)
        self.assertEqual(params["past_days"], 1)

    def test_out_of_range_dates_do_not_request_weather(self):
        for offset in (-1, 8):
            with self.assertRaises(ValueError):
                self.fetch(offset)
        self.client.get.assert_not_awaited()

    def test_missing_weather_is_unavailable_not_baseline(self):
        self.daily["precipitation_sum"][8] = None
        with self.assertRaises(RuntimeError):
            self.fetch(7)

    def test_missing_preceding_day_is_unavailable(self):
        self.daily["time"][0] = "2000-01-01"
        with self.assertRaises(RuntimeError):
            self.fetch(0)

    def test_provider_failure_does_not_create_forecast(self):
        self.response.raise_for_status.side_effect = RuntimeError("Provider unavailable")
        with self.assertRaises(RuntimeError):
            self.fetch(1)
        self.assertEqual(self.scope["_FORECAST_CACHE"], {})

    def test_model_receives_date_specific_weather_and_does_not_write_records(self):
        pipeline = Mock()
        pipeline.predict_proba.return_value = [[0.2, 0.8]]
        live = AsyncMock()
        weather = AsyncMock(return_value={"rainfall_24h_mm": 12, "rainfall_48h_mm": 20,
            "temperature_celsius": 26, "humidity_percent": 88, "source": "open-meteo-forecast"})
        database = Mock(side_effect=AssertionError("Unexpected database access"))
        scope = load_functions("services/forecast_service.py", {"predict_station_risk"},
            get_supabase=database, get_live_weather=live, get_forecast_weather=weather,
            **hazard_dependencies(), encode_source_type=lambda *_: 0,
            load_model_payload=lambda: {"pipeline": pipeline})
        result = asyncio.run(scope["predict_station_risk"]("station-1",
            forecast_date=self.days[8], station_data={"latitude": 10.13, "longitude": 124.87},
            config={"risk_threshold_low": 0.3, "risk_threshold_high": 0.65}))
        pipeline.predict_proba.assert_called_once_with([[0, 0, 12, 20, 26, 88, 0]])
        self.assertEqual(result["forecast_date"], self.days[8])
        self.assertEqual(result["predicted_status"], "undrinkable")
        live.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
