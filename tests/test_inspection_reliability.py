import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class InspectionReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tables = {}
        for name in ('inspection_requests', 'water_locations', 'users'):
            query = Mock()
            for method in ('select', 'eq', 'is_', 'update', 'insert', 'limit'):
                getattr(query, method).return_value = query
            self.tables[name] = query
        self.current = dict(id='ticket', status='pending', assigned_to=None,
                            barangay='Hantag', location_id='source')
        self.tables['inspection_requests'].execute.return_value.data = [self.current]
        self.tables['water_locations'].execute.return_value.data = [dict(
            id='source', barangay='Hantag', latitude=10.1, longitude=124.8)]
        self.db = SimpleNamespace(table=lambda name: self.tables[name])
        self.scope = load_functions('routers/inspections.py', {'create_inspection', 'update_inspection'},
                                   get_supabase=lambda: self.db, HTTPException=HTTPException, notify_staff=AsyncMock(), notify_linked_resident_outcome=AsyncMock())
        self.actor = dict(id='inspector', role='sanitization_inspector')

    def update(self, patch):
        return asyncio.run(self.scope['update_inspection']('ticket', patch, self.actor))

    def create(self, barangay='Hantag', source='source'):
        body = SimpleNamespace(location_id=source, description='Check water', barangay='Other', latitude=0, longitude=0)
        return asyncio.run(self.scope['create_inspection'](body, dict(id='resident', role='resident', barangay=barangay)))

    def test_create_uses_verified_source_coordinates_and_barangay(self):
        self.create()
        inserted = self.tables['inspection_requests'].insert.call_args.args[0]
        self.assertEqual(inserted['barangay'], 'Hantag')
        self.assertEqual(inserted['latitude'], 10.1)
        self.tables['water_locations'].is_.assert_called_with('archived_at', 'null')

    def test_create_rejects_missing_assignment_source_or_wrong_jurisdiction(self):
        for barangay, source, code in [(None, 'source', 403), ('Hantag', None, 422), ('Batuan', 'source', 403)]:
            with self.subTest(code=code), self.assertRaises(HTTPException) as error:
                self.create(barangay, source)
            self.assertEqual(error.exception.status_code, code)
        self.tables['inspection_requests'].insert.assert_not_called()

    def test_create_rejects_unavailable_source(self):
        self.tables['water_locations'].execute.return_value.data = []
        with self.assertRaises(HTTPException) as error:
            self.create()
        self.assertEqual(error.exception.status_code, 404)

    def test_processing_cannot_go_backwards(self):
        self.current['status'] = 'in_progress'
        for status in ('pending', 'assigned'):
            with self.assertRaises(HTTPException) as error:
                self.update({'status': status})
            self.assertEqual(error.exception.status_code, 409)
        self.tables['inspection_requests'].update.assert_not_called()

    def test_closed_retry_is_idempotent_but_changes_are_rejected(self):
        self.current['status'] = 'completed'
        self.assertTrue(self.update({'status': 'done'})['success'])
        with self.assertRaises(HTTPException):
            self.update({'status': 'completed', 'notes': 'Replace closed notes'})
        self.tables['inspection_requests'].update.assert_not_called()

    def test_inspector_cannot_reassign_to_someone_else(self):
        with self.assertRaises(HTTPException) as error:
            self.update({'assigned_to': 'other'})
        self.assertEqual(error.exception.status_code, 403)

    def test_relink_rejects_archived_and_cross_barangay_sources(self):
        for rows, code in [([], 404), ([{'barangay': 'Other'}], 403)]:
            self.tables['water_locations'].execute.return_value.data = rows
            with self.assertRaises(HTTPException) as error:
                self.update({'location_id': 'other'})
            self.assertEqual(error.exception.status_code, code)
        self.tables['inspection_requests'].update.assert_not_called()

    def test_linked_concern_source_cannot_be_replaced(self):
        self.current['resident_report_id'] = 'report'
        with self.assertRaises(HTTPException) as error:
            self.update({'location_id': 'different'})
        self.assertEqual(error.exception.status_code, 409)

    def test_update_guards_assignment_even_when_status_is_unchanged(self):
        self.update({'notes': 'Update'})
        self.tables['inspection_requests'].is_.assert_called_with('assigned_to', 'null')
        self.current['assigned_to'] = 'inspector'
        self.update({'notes': 'Another update'})
        self.tables['inspection_requests'].eq.assert_any_call('assigned_to', 'inspector')


if __name__ == '__main__':
    unittest.main()
