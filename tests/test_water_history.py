import asyncio
import unittest
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from test_push_delivery import load_functions


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


class WaterHistoryAPITests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.table.return_value.select.return_value.eq.return_value.is_.return_value.limit.return_value.execute.return_value.data = [{
            'id': 'station', 'barangay': 'Batuan', 'status': 'safe'}]
        self.db.rpc.return_value.execute.return_value.data = {'id': 'station', 'status': 'safe'}
        self.scope = load_functions('routers/water_locations.py',
            {'update_water_location', 'get_water_location_history', 'normalize_source_type', 'create_water_location'},
            get_supabase=lambda: self.db, HTTPException=HTTPException, timezone=timezone, timedelta=timedelta)
        self.user = dict(id='editor', role='sanitization_inspector')

    def update(self, values):
        body = SimpleNamespace(model_dump=lambda **_: values.copy())
        with patch.dict('sys.modules', {'services.heatmap_service': SimpleNamespace(recompute_heatmaps=AsyncMock())}):
            return asyncio.run(self.scope['update_water_location']('station', body, self.user))

    def test_update_preserves_explicit_nulls_cleared_notes_and_selected_effective_date(self):
        result = self.update(dict(history_date='2025-10-29', sample_date='2025-10-27',
            status='unknown', e_coli=None, notes='', description='', e_coli_count=0))
        args = self.db.rpc.call_args.args
        self.assertEqual(args[0], 'update_water_location_history')
        self.assertEqual(args[1]['p_effective_date'], '2025-10-29')
        self.assertIsNone(args[1]['p_patch']['e_coli'])
        self.assertEqual(args[1]['p_patch']['notes'], '')
        self.assertNotIn('history_date', args[1]['p_patch'])
        self.assertNotIn('description', args[1]['p_patch'])
        self.assertEqual(result['data']['status'], 'safe', 'API returns effective current state, not backdated edit')
        self.db.table.return_value.update.assert_not_called()
        self.db.table.return_value.delete.assert_not_called()

    def test_atomic_save_failure_is_not_reported_as_success(self):
        self.db.rpc.return_value.execute.side_effect = RuntimeError('Database failure')
        with self.assertRaises(HTTPException):
            self.update(dict(status='safe', history_date='2025-10-28'))
        self.db.table.return_value.update.assert_not_called()

    def test_source_type_is_saved_in_atomic_patch_and_omission_preserves_it(self):
        for supplied, expected in [('spring', 'spring'), ('piped_water', 'piped_tap'), ('unknown', 'unknown')]:
            self.update({'source_type': supplied})
            self.assertEqual(self.db.rpc.call_args.args[1]['p_patch']['source_type'], expected)
        self.update({'type': 'shallow_well'})
        self.assertEqual(self.db.rpc.call_args.args[1]['p_patch']['source_type'], 'shallow_well')
        self.update({'notes': 'Only notes'})
        self.assertNotIn('source_type', self.db.rpc.call_args.args[1]['p_patch'])

    def test_invalid_source_type_is_rejected_before_save(self):
        with self.assertRaises(HTTPException) as error:
            self.update({'source_type': 'misspelled'})
        self.assertEqual(error.exception.status_code, 422)
        self.db.rpc.assert_not_called()

    def test_create_persists_selected_source_type(self):
        class Body(SimpleNamespace):
            def __getattr__(self, name):
                return None
        self.db.table.return_value.insert.return_value.execute.return_value.data = [{'id': 'station'}]
        for selected in ('spring', 'deep_well', 'shallow_well', 'piped_tap', 'refilling_station'):
            body = Body(full_name='Source', barangay='Batuan', latitude=10.13, longitude=124.87,
                        status='unknown', source_type=selected)
            with patch.dict('sys.modules', {'services.heatmap_service': SimpleNamespace(recompute_heatmaps=AsyncMock())}):
                asyncio.run(self.scope['create_water_location'](body, self.user))
            self.assertEqual(self.db.table.return_value.insert.call_args.args[0]['source_type'], selected)

    def test_barangay_official_cannot_change_other_barangays(self):
        self.user = dict(id='editor', role='barangay_official', barangay='Other')
        with self.assertRaises(HTTPException) as error:
            self.update(dict(notes='Changed'))
        self.assertEqual(error.exception.status_code, 403)
        self.db.rpc.assert_not_called()

    def test_history_get_preserves_explicit_sample_values_and_never_writes(self):
        log = dict(id='one', effective_date='2025-10-28', sample_date='2025-10-27',
                   sample_time='04:21:00', notes='', e_coli=False, recorded_at='2025-10-29T00:00:00Z')
        query = self.db.table.return_value.select.return_value.eq.return_value
        query.order.return_value.order.return_value.order.return_value.range.return_value.execute.return_value.data = [log]
        result = asyncio.run(self.scope['get_water_location_history']('station', self.user))
        self.assertEqual(result['data'], [log])
        self.db.table.return_value.update.assert_not_called()
        self.db.table.return_value.insert.assert_not_called()

    def test_history_failure_is_explicit_instead_of_fabricating_a_snapshot(self):
        query = self.db.table.return_value.select.return_value.eq.return_value
        query.order.side_effect = RuntimeError('Missing migration')
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.scope['get_water_location_history']('station', self.user))
        self.assertEqual(error.exception.status_code, 503)

    def test_archive_uses_atomic_rpc_and_never_deletes_station_or_photo(self):
        scope = load_functions('routers/water_locations.py', {'set_water_location_archive'},
            get_supabase=lambda: self.db, HTTPException=HTTPException)
        with patch.dict('sys.modules', {'services.heatmap_service': SimpleNamespace(recompute_heatmaps=AsyncMock())}):
            result = asyncio.run(scope['set_water_location_archive']('station', self.user, True))
        self.assertEqual(result['message'], 'Water source archived')
        self.db.rpc.assert_called_once_with('set_water_source_archived', {
            'p_location_id': 'station', 'p_actor_id': 'editor', 'p_archive': True})
        self.db.table.assert_not_called()

    def test_restore_uses_original_station_id(self):
        scope = load_functions('routers/water_locations.py', {'set_water_location_archive'},
            get_supabase=lambda: self.db, HTTPException=HTTPException)
        with patch.dict('sys.modules', {'services.heatmap_service': SimpleNamespace(recompute_heatmaps=AsyncMock())}):
            result = asyncio.run(scope['set_water_location_archive']('station', self.user, False))
        self.assertEqual(result['message'], 'Water source restored')
        self.assertEqual(self.db.rpc.call_args.args[1]['p_location_id'], 'station')
