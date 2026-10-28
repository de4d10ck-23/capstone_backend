"""Regression checks for missing jurisdiction and stale login sessions."""
import asyncio
import ast
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from test_notification_reads import HTTPException
from test_push_delivery import ROOT, load_functions


def database(rows):
    query = Mock()
    for method in ('select', 'eq', 'ilike', 'is_', 'order', 'limit'):
        getattr(query, method).return_value = query
    query.execute.return_value = SimpleNamespace(data=rows)
    db = Mock()
    db.table.return_value = query
    return db, query


class AuditAccessTests(unittest.TestCase):
    def test_missing_records_return_404_without_single_row_database_assertions(self):
        cases = [('routers/users.py', 'get_user'),
                 ('routers/users.py', 'toggle_user_active'),
                 ('routers/water_locations.py', 'get_water_location'),
                 ('routers/water_locations.py', 'get_water_location_history')]
        for path, name in cases:
            with self.subTest(route=name):
                db, query = database([])
                scope = load_functions(path, {name}, get_supabase=lambda: db, HTTPException=HTTPException)
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(scope[name]('missing', {'id': 'admin', 'role': 'admin'}))
                self.assertEqual(error.exception.status_code, 404)
                query.single.assert_not_called()

    def test_missing_barangay_never_executes_unscoped_query(self):
        for path, name in [
            ('routers/resident_reports.py', 'list_resident_reports'),
            ('routers/inspections.py', 'list_inspections'),
            ('routers/water_locations.py', 'list_water_locations'),
        ]:
            for barangay in (None, '', '   '):
                with self.subTest(route=name, barangay=barangay):
                    db, query = database([])
                    scope = load_functions(path, {name}, get_supabase=lambda: db,
                                           HTTPException=HTTPException)
                    with self.assertRaises(HTTPException) as error:
                        asyncio.run(scope[name](current_user={
                            'id': 'official', 'role': 'barangay_official', 'barangay': barangay}))
                    self.assertEqual(error.exception.status_code, 403)
                    query.execute.assert_not_called()

    def test_assigned_official_queries_only_assigned_barangay(self):
        for path, name, method in [
            ('routers/resident_reports.py', 'list_resident_reports', 'eq'),
            ('routers/inspections.py', 'list_inspections', 'eq'),
            ('routers/water_locations.py', 'list_water_locations', 'ilike'),
        ]:
            with self.subTest(route=name):
                db, query = database([])
                scope = load_functions(path, {name}, get_supabase=lambda: db,
                                       HTTPException=HTTPException)
                result = asyncio.run(scope[name](current_user={
                    'id': 'official', 'role': 'barangay_official', 'barangay': 'Hantag'}))
                self.assertTrue(result['success'])
                getattr(query, method).assert_called_with('barangay', 'Hantag')

    def test_deleted_and_inactive_accounts_have_explicit_auth_errors(self):
        for rows, expected in [([], 401), ([{'is_active': False}], 403),
                               ([{'id': 'user', 'is_active': True}], None)]:
            with self.subTest(expected=expected):
                db, _ = database(rows)
                scope = load_functions('core/dependencies.py', {'get_current_user'},
                    get_supabase=lambda: db, decode_access_token=lambda _: {'sub': 'user'},
                    HTTPException=HTTPException,
                    status=SimpleNamespace(HTTP_401_UNAUTHORIZED=401, HTTP_403_FORBIDDEN=403))
                call = scope['get_current_user'](SimpleNamespace(credentials='token'))
                if expected:
                    with self.assertRaises(HTTPException) as error:
                        asyncio.run(call)
                    self.assertEqual(error.exception.status_code, expected)
                else:
                    self.assertEqual(asyncio.run(call)['id'], 'user')

    def test_user_directory_is_admin_only(self):
        tree = ast.parse((ROOT / 'routers/users.py').read_text(encoding='utf-8'))
        route = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'list_users')
        self.assertIn('Depends(require_admin)', ast.unparse(route.args.args[0].annotation))


if __name__ == '__main__':
    unittest.main()
