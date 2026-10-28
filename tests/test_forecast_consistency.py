import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from test_push_delivery import load_functions


class ConsistencyTests(unittest.TestCase):
    def test_surveillance_counts_exact_saved_predictions_ignoring_draft_thresholds(self):
        predictions = [dict(station_id=str(i), station_name='Station', barangay='Batuan',
            predicted_status=status, risk_percentage=75, risk_level='High', stale=True,
            thresholds_used={'low_cutoff': .3, 'high_cutoff': .7})
            for i, status in enumerate(['safe', 'warning'] + ['undrinkable'] * 4)]
        read = Mock(return_value=dict(results=predictions, unavailable_station_ids=['missing'],
            forecast_date='2026-10-28', stale_count=6))
        push = AsyncMock()
        predict = AsyncMock(side_effect=AssertionError('Must not re-run inference'))
        scope = load_functions('services/forecast_service.py', {'run_barangay_surveillance_and_alert'},
            get_supabase=Mock(), _today_str=lambda: '2026-10-28',
            predict_station_risk=predict, _load_push_alert_log=lambda: {}, broadcast_push_notification=push)
        with patch.dict('sys.modules', {'services.saved_forecast_service': SimpleNamespace(get_saved_forecasts=read)}):
            result = asyncio.run(scope['run_barangay_surveillance_and_alert'](
                barangay='Batuan', custom_threshold_high=.99))
        self.assertEqual(result['contaminated_count'], 4)
        self.assertEqual(result['safe_count'], 1)
        self.assertEqual(result['warning_count'], 1)
        self.assertEqual(result['unavailable_count'], 1)
        self.assertEqual(result['results'], predictions)
        self.assertEqual(result['thresholds_used']['high_cutoff'], .7)
        read.assert_called_once_with('2026-10-28', 'Batuan')
        predict.assert_not_awaited()
        push.assert_not_awaited()

    def test_today_inference_uses_daily_weather_and_exact_threshold_boundaries(self):
        weather = AsyncMock(return_value=dict(rainfall_24h_mm=0, rainfall_48h_mm=0,
            temperature_celsius=0, humidity_percent=0, source='open-meteo-forecast'))
        pipeline = Mock()
        scope = load_functions('services/forecast_service.py', {'predict_station_risk'},
            _today_str=lambda: '2026-10-28', get_forecast_weather=weather,
            get_station_hazard_levels=lambda *_: (0, 0), encode_source_type=lambda *_: 1,
            load_model_payload=lambda: {'pipeline': pipeline})
        for probability, status in [(.2999, 'safe'), (.3, 'warning'), (.7, 'warning'), (.7001, 'undrinkable')]:
            with self.subTest(probability=probability):
                pipeline.predict_proba.return_value = [[1-probability, probability]]
                result = asyncio.run(scope['predict_station_risk']('one',
                    station_data=dict(latitude=10, longitude=124),
                    config=dict(risk_threshold_low=.3, risk_threshold_high=.7)))
                self.assertEqual(result['predicted_status'], status)
                self.assertEqual(result['forecast_date'], '2026-10-28')
                pipeline.predict_proba.assert_called_with([[0, 0, 0, 0, 0, 0, 1]])
        weather.assert_awaited_with('2026-10-28', 10, 124)
