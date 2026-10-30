import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class ReportSourceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.table.return_value.select.return_value.eq.return_value.is_.return_value.execute.return_value.data = [
            dict(id='station', full_name='Spring', barangay='Batuan', latitude=10.13, longitude=124.87)]
        self.db.table.return_value.insert.return_value.execute.return_value.data = [{'id': 'report'}]
        self.submit = load_functions('routers/resident_reports.py', {'submit_resident_report'},
            get_supabase=lambda: self.db, HTTPException=HTTPException, notify_staff=AsyncMock())['submit_resident_report']
        self.user = dict(id='resident', role='resident', barangay='Batuan')
        self.body = SimpleNamespace(location_id='station', category='request_inspection', type='concern',
            title='Inspection', description='Check water', image_url=None, photo_url=None,
            latitude=0, longitude=0, barangay='Spoofed barangay')

    def test_missing_selection_cannot_insert_a_report(self):
        self.body.location_id = None
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.submit(self.body, self.user))
        self.assertEqual(error.exception.status_code, 422)
        self.db.table.return_value.insert.assert_not_called()

    def test_archived_source_cannot_be_submitted(self):
        self.db.table.return_value.select.return_value.eq.return_value.is_.return_value.execute.return_value.data = []
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.submit(self.body, self.user))
        self.assertEqual(error.exception.status_code, 404)
        self.db.table.return_value.select.return_value.eq.return_value.is_.assert_called_once_with('archived_at', 'null')
        self.db.table.return_value.insert.assert_not_called()

    def test_source_in_another_barangay_is_rejected(self):
        self.user['barangay'] = 'Other'
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.submit(self.body, self.user))
        self.assertEqual(error.exception.status_code, 403)
        self.db.table.return_value.insert.assert_not_called()

    def test_valid_selection_uses_database_source_details(self):
        self.body.description += '\n[Inspected Source: Fake (ID: fake)]'
        self.assertTrue(asyncio.run(self.submit(self.body, self.user))['success'])
        saved = self.db.table.return_value.insert.call_args.args[0]
        self.assertEqual(saved['barangay'], 'Batuan')
        self.assertEqual((saved['latitude'], saved['longitude']), (10.13, 124.87))
        self.assertIn('[Inspected Source: Spring (ID: station)]', saved['description'])
        self.assertNotIn('Fake', saved['description'])

    def test_add_unregistered_source_still_works_without_existing_station(self):
        self.body.location_id = None
        self.body.category = self.body.type = 'unregistered_source'
        self.assertTrue(asyncio.run(self.submit(self.body, self.user))['success'])
        self.db.table.return_value.select.assert_not_called()

    def test_barangay_concern_keeps_category_source_description_and_photo(self):
        self.user['role'] = 'barangay_official'
        self.body.category = self.body.type = 'broken_pipe'
        self.body.description = 'The distribution pipe is damaged.'
        self.body.image_url = 'https://example.com/proof.jpg'
        asyncio.run(self.submit(self.body, self.user))
        saved = self.db.table.return_value.insert.call_args.args[0]
        self.assertEqual(saved['status'], 'validated')
        self.assertEqual(saved['type'], 'broken_pipe')
        self.assertEqual(saved['submitted_by'], 'resident')
        self.assertIn('The distribution pipe is damaged.', saved['description'])
        self.assertIn('[Inspected Source: Spring (ID: station)]', saved['description'])
        self.assertIn('[Attached Photo Proof: https://example.com/proof.jpg]', saved['description'])
        self.assertEqual(saved['barangay'], 'Batuan')

    def test_barangay_concerns_require_a_source_too(self):
        self.user['role'] = 'barangay_official'
        self.body.location_id = None
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.submit(self.body, self.user))
        self.assertEqual(error.exception.status_code, 422)
        self.db.table.return_value.insert.assert_not_called()
