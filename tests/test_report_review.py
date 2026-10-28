import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class ReportReviewTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.record = dict(id='report', generated_by='author', submitted_at='2026-10-28', review_status='submitted')
        self.scope = load_functions('routers/reports.py', {'report_transition'},
            saved_report=lambda *_: self.record, get_supabase=lambda: self.db,
            HTTPException=HTTPException, logger=logging.getLogger(__name__))

    def reject(self, user, action, status, notes=None):
        with self.assertRaises(HTTPException) as error:
            self.scope['report_transition']('report', user, action, notes)
        self.assertEqual(error.exception.status_code, status)
        self.db.rpc.assert_not_called()

    def test_other_author_cannot_send(self):
        self.reject(dict(id='other', role='sanitization_inspector'), 'send', 403)

    def test_empty_review_notes_cannot_complete(self):
        self.reject(dict(id='doctor', role='city_health_officer'), 'complete', 422, '   ')

    def test_other_doctor_cannot_take_over_active_review(self):
        self.record.update(review_status='in_review', review_started_by='doctor1')
        self.reject(dict(id='doctor2', role='city_health_officer'), 'complete', 409, 'Reviewed')

    def test_rpc_receipt_omits_snapshot_and_uses_authenticated_actor(self):
        self.db.rpc.return_value.execute.return_value.data = dict(id='report', snapshot={'private': True}, review_status='submitted')
        result = self.scope['report_transition']('report', dict(id='author', role='sanitization_inspector'), 'send')
        self.db.rpc.assert_called_once_with('send_report_to_chu', {'p_report_id': 'report', 'p_actor_id': 'author'})
        self.assertNotIn('snapshot', result['data'])

    def test_known_database_conflict_is_reported_as_conflict(self):
        self.db.rpc.return_value.execute.side_effect = Exception('Start review before marking the report Done')
        with self.assertRaises(HTTPException) as error:
            self.scope['report_transition']('report', dict(id='doctor', role='city_health_officer'), 'complete', 'Reviewed')
        self.assertEqual(error.exception.status_code, 409)

    def test_doctor_cannot_open_unsent_report(self):
        self.db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [dict(id='report')]
        scope = load_functions('routers/reports.py', {'saved_report'}, get_supabase=lambda: self.db,
            HTTPException=HTTPException, snapshot_hash=Mock())
        with self.assertRaises(HTTPException) as error:
            scope['saved_report']('report', dict(id='doctor', role='city_health_officer'))
        self.assertEqual(error.exception.status_code, 403)
        scope['snapshot_hash'].assert_not_called()

    def test_doctor_list_filters_to_received_reports(self):
        query = self.db.table.return_value.select.return_value.order.return_value.order.return_value
        scope = load_functions('routers/reports.py', {'list_reports'}, get_supabase=lambda: self.db,
            HTTPException=HTTPException, read_all=lambda _: [], logger=logging.getLogger(__name__))
        asyncio.run(scope['list_reports'](dict(role='city_health_officer')))
        query.in_.assert_called_once_with('review_status', ['submitted', 'in_review', 'done'])

    def test_completion_endpoint_passes_only_trimmed_notes_and_authenticated_user(self):
        transition = Mock(return_value={'success': True})
        scope = load_functions('routers/reports.py', {'complete_report_review'}, report_transition=transition)
        user = dict(id='doctor', role='city_health_officer')
        asyncio.run(scope['complete_report_review']('report', SimpleNamespace(notes=' Reviewed '), user))
        transition.assert_called_once_with('report', user, 'complete', 'Reviewed')
