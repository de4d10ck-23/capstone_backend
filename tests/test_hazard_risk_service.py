import asyncio
import json
import math
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from hazard_test_helpers import hazard_dependencies
from services import hazard_risk_service as service
from shapely.geometry import box, mapping, shape
from test_push_delivery import load_functions

LAT, LNG = 10.18, 124.83


def zone(code, inferred=False, barangay='Hantag'):
    return {'geometry': mapping(box(LNG-.001, LAT-.001, LNG+.001, LAT+.001)),
            'properties': dict(code=code, kind='flood' if code == 'F' else 'coverage' if code == 'OUT' else 'landslide',
                               barangay=barangay, inferred=inferred, source='Test geometry')}


def source(kind='septic_tank', **kwargs):
    return dict(id='test', name='Test source', hazard_type=kind, barangay='Hantag',
                geometry_type='Point', coordinates=[LNG, LAT], **kwargs)


class HazardRiskTests(unittest.TestCase):
    def assess(self, features=(), records=(), barangay='Hantag'):
        return service.assess_station_hazards(LAT, LNG, barangay, records, features=features)

    def test_each_natural_class_has_its_own_weight_and_flood_has_no_invented_severity(self):
        weights = [self.assess([zone(code)])['combined_weight'] for code in ['OUT', 'LL', 'ML', 'HL', 'F']]
        self.assertEqual(weights, [0, .03, .10, .20, .30])
        self.assertEqual(self.assess([zone('F')])['flood_label'], 'Flood susceptible')
        self.assertEqual(self.assess([zone('OUT')])['coverage'], 'outside_assessed')

    def test_inferred_is_downweighted_and_duplicate_polygons_do_not_stack(self):
        self.assertEqual(self.assess([zone('HL', True)])['combined_weight'], .10)
        self.assertEqual(self.assess([zone('HL'), zone('HL'), zone('HL', True)])['combined_weight'], .20)
        self.assertEqual(self.assess([zone('ML'), zone('HL')])['combined_weight'], .20)

    def test_all_source_categories_have_distinct_explicit_weights(self):
        for kind, weight in [('poultry_farm', .20), ('septic_tank', .25), ('farmland', .10),
                             ('wastewater', .22), ('drainage', .08), ('unknown', 0)]:
            with self.subTest(kind=kind):
                assessment = self.assess(records=[source(kind)])
                self.assertAlmostEqual(assessment['combined_weight'], weight)

    def test_nearby_sources_decay_and_use_designated_barangay(self):
        close = source()
        far = {**close, 'coordinates': [LNG, LAT+75/111320]}
        distant = {**close, 'coordinates': [LNG, LAT+.02]}
        wrong = {**close, 'barangay': 'Rizal'}
        unassigned = {**close, 'barangay': None}
        self.assertAlmostEqual(self.assess(records=[far])['combined_weight'], .125, places=5)
        self.assertEqual(self.assess(records=[distant, wrong, unassigned])['combined_weight'], 0)
        self.assertEqual(self.assess(records=[close, close, far])['combined_weight'], .25)
        self.assertEqual(self.assess(records=[close], barangay='Brgy. Hantag')['combined_weight'], .25)
        self.assertEqual(self.assess([zone('HL')], barangay='Rizal')['coverage'], 'no_data')

    def test_polygon_holes_and_lines_have_real_metric_distances(self):
        ring = mapping(box(LNG-.004, LAT-.004, LNG+.004, LAT+.004))['coordinates'][0]
        hole = mapping(box(LNG-.002, LAT-.002, LNG+.002, LAT+.002))['coordinates'][0]
        polygon = {**source(), 'geometry_type': 'Polygon', 'coordinates': [ring, hole]}
        self.assertEqual(self.assess(records=[polygon])['combined_weight'], 0)
        line = {**source('river'), 'geometry_type': 'LineString', 'coordinates': [[LNG, LAT-.01], [LNG, LAT+.01]]}
        self.assertEqual(self.assess(records=[line])['combined_weight'], .08)

    def test_unknown_is_not_safe_and_missing_data_does_not_load_legacy_files(self):
        result = self.assess()
        self.assertEqual(result['coverage'], 'no_data')
        self.assertIn('Unknown', result['landslide_label'])
        with patch.object(service, 'STUDY_PATH', Path('missing-study-map.geojson')):
            with self.assertRaises(FileNotFoundError):
                service.load_study_data()
        with self.assertRaises(ValueError):
            service.assess_station_hazards(math.nan, LNG, 'Hantag', [], features=[])

    def test_rain_adjustment_is_bounded_monotonic_and_explicitly_uncalibrated(self):
        assessment = self.assess([zone('F'), zone('HL')], [source(), source('poultry_farm')])
        self.assertEqual(assessment['combined_weight'], .65)
        dry = service.apply_hazard_adjustment(.2, assessment, 0)
        wet = service.apply_hazard_adjustment(.2, assessment, 30)
        self.assertEqual(dry['risk_score'], .46)
        self.assertEqual(wet['risk_score'], .72)
        self.assertEqual(service.apply_hazard_adjustment(.2, assessment, 300)['risk_score'], .72)
        self.assertEqual(service.apply_hazard_adjustment(1, assessment, 30)['risk_score'], 1)
        self.assertEqual(service.apply_hazard_adjustment(.2, self.assess(), 30)['risk_score'], .2)
        self.assertEqual(wet['score_kind'], 'uncalibrated_hybrid_risk_index')

    def test_backend_and_staff_ship_identical_new_dataset_including_estimates(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(service.STUDY_PATH.read_bytes(), (root/'frontend/staff/public/data/maasin_study_hazards.geojson').read_bytes())
        data = service.load_study_data()
        estimate = next(f for f in data['features'] if f['properties'].get('inferred') and f['properties']['code'] == 'HL')
        point = shape(estimate['geometry']).representative_point()
        result = service.assess_station_hazards(point.y, point.x, estimate['properties']['barangay'], [])
        self.assertTrue(any(c['inferred'] for c in result['contributions']))

    def test_forecast_uses_new_weights_without_legacy_hazard_double_counting(self):
        pipeline = Mock()
        pipeline.predict_proba.return_value = [[.8, .2]]
        dependencies = hazard_dependencies()
        dependencies['assess_station_hazards'] = lambda *args: self.assess([zone('F')], [source()])
        scope = load_functions('services/forecast_service.py', {'predict_station_risk'},
            **dependencies, encode_source_type=lambda *_: 1, load_model_payload=lambda: {'pipeline': pipeline},
            get_forecast_weather=AsyncMock(return_value=dict(rainfall_24h_mm=15, rainfall_48h_mm=30,
                temperature_celsius=28, humidity_percent=80)))
        result = asyncio.run(scope['predict_station_risk']('one', forecast_date='2026-10-30',
            station_data=dict(latitude=LAT, longitude=LNG, barangay='Hantag'),
            config=dict(risk_threshold_low=.3, risk_threshold_high=.65)))
        pipeline.predict_proba.assert_called_once_with([[0, 0, 15, 30, 28, 80, 1]])
        self.assertEqual(result['risk_probability'], .64)
        self.assertEqual(result['risk_breakdown']['hazard_adjustment'], .44)
        self.assertEqual(result['predicted_status'], 'warning')
        self.assertEqual(result['hazard_model_version'], service.HAZARD_MODEL_VERSION)

    def test_heatmap_uses_same_adjustment_but_keeps_inspector_status(self):
        pipeline = Mock()
        pipeline.predict_proba.return_value = [[.8, .2]]
        station = dict(id='one', latitude=LAT, longitude=LNG, barangay='Hantag', status='safe')
        query = Mock()
        query.select.return_value.is_.return_value.execute.return_value.data = [station]
        sb = SimpleNamespace(table=lambda _: query)
        assessment = self.assess([zone('F')], [source()])
        scope = load_functions('services/heatmap_service.py', {'generate_water_contamination_heatmap_geojson'},
            get_supabase=lambda: sb, fetch_hazard_records=lambda: [],
            assess_station_hazards=lambda *_: assessment, apply_hazard_adjustment=service.apply_hazard_adjustment,
            get_live_weather=AsyncMock(return_value=dict(rainfall_24h_mm=15, rainfall_48h_mm=30,
                temperature_celsius=28, humidity_percent=80)),
            MAASIN_DEFAULT_LAT=LAT, MAASIN_DEFAULT_LNG=LNG, encode_source_type=lambda *_: 1,
            load_model_payload=lambda: {'pipeline': pipeline}, get_barangay_population=lambda _: 100,
            MAX_BARANGAY_POPULATION=1000)
        geojson, stats = asyncio.run(scope['generate_water_contamination_heatmap_geojson']())
        pipeline.predict_proba.assert_called_once_with([[0, 0, 15, 30, 28, 80, 1]])
        props = geojson['features'][0]['properties']
        self.assertEqual(props['risk_probability'], .64)
        self.assertEqual(props['status'], 'safe')
        self.assertEqual(stats['safe'], 1)
        self.assertEqual(props['hazard_assessment'], assessment)

    def test_inventory_reads_all_pages_and_errors_are_not_empty_inventory(self):
        query = Mock()
        query.select.return_value.order.return_value.range.return_value.execute.side_effect = [
            SimpleNamespace(data=[source() for _ in range(500)]), SimpleNamespace(data=[source()])]
        sb = SimpleNamespace(table=lambda _: query)
        with patch.dict('sys.modules', {'core.database': SimpleNamespace(get_supabase=lambda: sb)}):
            self.assertEqual(len(service.fetch_hazard_records()), 501)
            query.select.return_value.order.return_value.range.return_value.execute.side_effect = RuntimeError('Offline')
            with self.assertRaises(RuntimeError):
                service.fetch_hazard_records()


if __name__ == '__main__':
    unittest.main()
