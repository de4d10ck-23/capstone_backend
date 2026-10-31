"""Timeline weather preserves dates and missing conditions without guessing sunshine."""
import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_push_delivery import load_functions


class WeatherTimelineTests(unittest.TestCase):
    def setUp(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        self.dates = [(today + timedelta(days=i)).isoformat() for i in range(8)]
        self.codes = [0, 1, 2, 3, 61, 80, 95, None]
        self.response = Mock()
        self.response.json.return_value = {"daily": {"time": self.dates, "weather_code": self.codes}}
        self.client = AsyncMock()
        self.client.get.return_value = self.response
        context = AsyncMock()
        context.__aenter__.return_value = self.client
        self.cache = {}
        self.scope = load_functions("services/weather_service.py", {"get_forecast_weather_timeline"},
            timedelta=timedelta, timezone=timezone, time=time, _TIMELINE_WEATHER_CACHE=self.cache,
            CACHE_TTL_SECONDS=600, MAASIN_DEFAULT_LAT=10.133, MAASIN_DEFAULT_LNG=124.87,
            httpx=SimpleNamespace(AsyncClient=lambda **_: context))

    def fetch(self):
        return asyncio.run(self.scope["get_forecast_weather_timeline"]())

    def test_eight_dates_preserve_clear_zero_and_missing_weather_and_use_cache(self):
        result = self.fetch()
        self.assertEqual([day["date"] for day in result["days"]], self.dates)
        self.assertEqual([day["weather_code"] for day in result["days"]], self.codes)
        self.assertEqual(self.fetch(), result)
        self.client.get.assert_awaited_once()
        self.assertEqual(self.client.get.call_args.kwargs["params"]["daily"], "weather_code")

    def test_missing_day_does_not_shift_following_conditions(self):
        self.response.json.return_value = {"daily": {"time": self.dates[1:], "weather_code": self.codes[1:]}}
        result = self.fetch()
        self.assertIsNone(result["days"][0]["weather_code"])
        self.assertEqual(result["days"][6]["weather_code"], 95)

    def test_empty_data_and_provider_failure_are_not_cached(self):
        self.response.json.return_value = {"daily": {}}
        with self.assertRaises(RuntimeError):
            self.fetch()
        self.assertEqual(self.cache, {})
        self.client.get.side_effect = RuntimeError("Provider offline")
        with self.assertRaises(RuntimeError):
            self.fetch()
        self.assertEqual(self.cache, {})
