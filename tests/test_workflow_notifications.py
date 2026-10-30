import asyncio
import unittest
from unittest.mock import Mock, AsyncMock
from test_push_delivery import load_functions


class WorkflowNotificationTests(unittest.TestCase):
    def test_staff_recipient_scope_and_routes(self):
        db = Mock()
        query = db.table.return_value.select.return_value.in_.return_value.eq.return_value.order.return_value
        query.range.return_value.execute.return_value.data = [
            dict(id='correct', role='barangay_official', barangay=' Hantag '),
            dict(id='other', role='barangay_official', barangay='Batuan'),
            dict(id='missing', role='barangay_official', barangay=None)]
        push = AsyncMock()
        scope = load_functions('services/workflow_notifications.py', {'notify_staff'},
                               get_supabase=lambda: db, broadcast_push_notification=push)
        asyncio.run(scope['notify_staff'](['barangay_official'], 'New', 'Review', '/validate-reports', 'event', 'hantag'))
        push.assert_awaited_once_with('New', 'Review', user_id='correct', url='/validate-reports', tag='event')
        query.range.assert_called_once_with(0, 499)

    def test_outcome_push_only_to_submitter(self):
        push = AsyncMock()
        scope = load_functions('services/workflow_notifications.py', {'notify_resident_outcome'}, broadcast_push_notification=push)
        for status in ['done', 'dismissed']:
            asyncio.run(scope['notify_resident_outcome'](dict(id='r', status=status, submitted_by='owner', type='unregistered_source')))
            self.assertEqual(push.call_args.kwargs['user_id'], 'owner')
            self.assertIn(status, push.call_args.args[0])
            self.assertIn('Water source', push.call_args.args[0])
        push.reset_mock()
        asyncio.run(scope['notify_resident_outcome'](dict(id='r', status='pending', submitted_by='owner')))
        push.assert_not_awaited()

    def test_private_feed_visibility(self):
        visible = load_functions('routers/notifications.py', {'notification_visible_to'})['notification_visible_to']
        row = dict(target_user_id='owner', type='report_status')
        self.assertTrue(visible(row, dict(id='owner', role='resident')))
        for user in [None, dict(id='other', role='resident'), dict(id='admin', role='admin')]:
            self.assertFalse(visible(row, user))
        self.assertFalse(visible(dict(type='report_status'), dict(id='owner', role='resident')))
        self.assertFalse(visible(dict(target_roles=['city_health_officer']), dict(role='resident')))
        self.assertTrue(visible(dict(target_roles=['city_health_officer']), dict(role='city_health_officer')))

    def test_push_failure_does_not_fail_saved_report(self):
        push = AsyncMock(side_effect=RuntimeError('offline'))
        scope = load_functions('services/workflow_notifications.py', {'notify_resident_outcome'}, broadcast_push_notification=push)
        asyncio.run(scope['notify_resident_outcome'](dict(id='r', status='done', submitted_by='owner')))
