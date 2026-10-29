import asyncio
import unittest
from unittest.mock import Mock

from test_notification_reads import HTTPException
from test_push_delivery import load_functions


class ProtectedAdminTests(unittest.TestCase):
    def setUp(self):
        self.query = Mock()
        for method in ('select', 'eq', 'limit', 'delete'):
            getattr(self.query, method).return_value = self.query
        db = Mock()
        db.table.return_value = self.query
        self.delete = load_functions('routers/users.py', {'delete_user'},
            get_supabase=lambda: db, HTTPException=HTTPException)['delete_user']

    def test_admin_username_cannot_be_deleted(self):
        for username in ('admin', 'ADMIN', ' Admin '):
            self.query.execute.return_value.data = [{'username': username}]
            with self.assertRaises(HTTPException) as error:
                asyncio.run(self.delete('target', {'role': 'admin'}))
            self.assertEqual(error.exception.status_code, 403)
        self.query.delete.assert_not_called()

    def test_other_user_can_be_deleted_with_username_guard(self):
        self.query.execute.return_value.data = [{'username': 'resident'}]
        result = asyncio.run(self.delete('target', {'role': 'admin'}))
        self.assertTrue(result['success'])
        self.query.delete.assert_called_once()
        self.query.eq.assert_any_call('username', 'resident')

    def test_missing_user_not_deleted(self):
        self.query.execute.return_value.data = []
        with self.assertRaises(HTTPException) as error:
            asyncio.run(self.delete('missing', {'role': 'admin'}))
        self.assertEqual(error.exception.status_code, 404)
        self.query.delete.assert_not_called()
