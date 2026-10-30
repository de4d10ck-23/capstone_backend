"""Authenticated queue counts; never load report bodies or send notifications."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class PendingReportCountTests(unittest.TestCase):
    def setUp(self):
        self.query = Mock()
        for method in ('select', 'eq', 'or_', 'is_'):
            getattr(self.query, method).return_value = self.query
        self.query.not_ = self.query
        self.query.execute.return_value = SimpleNamespace(count=2501)
        self.db = Mock()
        self.db.table.return_value = self.query
        self.scope = load_functions('routers/reports.py', {'pending_report_count'},
            get_supabase=lambda: self.db, HTTPException=HTTPException)

    def count(self, role, **fields):
        return asyncio.run(self.scope['pending_report_count'](dict(id='staff', role=role, **fields)))

    def test_barangay_count_is_exact_scoped_and_excludes_registry_submissions(self):
        result = self.count('barangay_official', barangay='Hantag')
        self.db.table.assert_called_once_with('resident_reports')
        self.query.select.assert_called_once_with('id', count='exact', head=True)
        self.query.eq.assert_called_once_with('barangay', 'Hantag')
        expression = self.query.or_.call_args.args[0]
        self.assertIn('type.neq.unregistered_source', expression)
        self.assertIn('status.eq.pending', expression)
        self.assertNotIn('status.eq.validated', expression)
        self.assertEqual(result, dict(success=True, count=2501, route='/validate-reports'))

    def test_missing_barangay_cannot_fall_back_to_city_wide_count(self):
        for barangay in (None, '', '   '):
            with self.assertRaises(HTTPException) as caught:
                self.count('barangay_official', barangay=barangay)
            self.assertEqual(caught.exception.status_code, 403)
        self.db.table.assert_not_called()

    def test_inspector_counts_pending_tickets_not_duplicate_linked_reports(self):
        result = self.count('sanitization_inspector')
        self.db.table.assert_called_once_with('inspection_requests')
        expression = self.query.or_.call_args.args[0]
        self.assertIn('status.eq.pending', expression)
        self.assertIn('status.eq.assigned', expression)
        self.assertNotIn('in_progress', expression)
        self.assertEqual(result['route'], '/inspections')

    def test_doctor_counts_only_submitted_reports_not_drafts_or_started_reviews(self):
        result = self.count('city_health_officer')
        self.db.table.assert_called_once_with('reports')
        self.query.eq.assert_called_once_with('review_status', 'submitted')
        self.query.is_.assert_called_once_with('submitted_at', 'null')
        self.assertEqual(result['route'], '/view-reports')

    def test_other_roles_are_rejected_without_database_access(self):
        for role in ('resident', 'admin', None):
            with self.assertRaises(HTTPException) as caught:
                self.count(role)
            self.assertEqual(caught.exception.status_code, 403)
        self.db.table.assert_not_called()

    def test_empty_queue_is_zero_but_missing_count_or_database_failure_is_unavailable(self):
        self.query.execute.return_value.count = 0
        self.assertEqual(self.count('city_health_officer')['count'], 0)
        for invalid in (None, -1, True, '12'):
            self.query.execute.return_value.count = invalid
            with self.assertRaises(HTTPException) as caught:
                self.count('city_health_officer')
            self.assertEqual(caught.exception.status_code, 503)
        self.query.execute.side_effect = RuntimeError('Offline')
        with self.assertRaises(HTTPException) as caught:
            self.count('city_health_officer')
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
