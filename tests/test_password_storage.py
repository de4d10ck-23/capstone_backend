import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from test_push_delivery import load_functions
from test_notification_reads import HTTPException


class PasswordStorageTests(unittest.TestCase):
    def test_admin_and_resident_creation_store_hash_only(self):
        for path, name in [('routers/auth.py', 'register'), ('routers/users.py', 'create_user')]:
            with self.subTest(name=name):
                db = Mock()
                query = db.table.return_value
                query.select.return_value.eq.return_value.execute.return_value.data = []
                query.select.return_value.ilike.return_value.execute.return_value.data = []
                query.insert.return_value.execute.return_value.data = [{'id': 'new', 'password_hash': 'hashed'}]
                hash_fn = Mock(return_value='hashed')
                fn = load_functions(path, {name}, get_supabase=lambda: db,
                    hash_password=hash_fn, HTTPException=HTTPException,
                    validate_and_normalize_barangay=lambda value: value, VALID_ROLES={'resident'})[name]
                body = SimpleNamespace(username='example', full_name='Example Resident', email='example@test.com',
                    password='secret-value', barangay='Combado', role='resident', is_active=True)
                result = asyncio.run(fn(body) if name == 'register' else fn(body, {'role': 'admin'}))
                hash_fn.assert_called_once_with('secret-value')
                stored = query.insert.call_args.args[0]
                self.assertEqual(stored['password_hash'], 'hashed')
                self.assertNotIn('password', stored)
                self.assertNotIn('password_hash', result['user'])

    def test_admin_password_update_stores_hash_only(self):
        db = Mock()
        db.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{'id': 'account', 'password_hash': 'hashed'}]
        hash_fn = Mock(return_value='hashed')
        fn = load_functions('routers/users.py', {'update_user'}, get_supabase=lambda: db,
            hash_password=hash_fn, HTTPException=HTTPException)['update_user']
        body = SimpleNamespace(model_dump=lambda: {'password': 'secret-value'})
        result = asyncio.run(fn('account', body, {'role': 'admin'}))
        db.table.return_value.update.assert_called_once_with({'password_hash': 'hashed'})
        hash_fn.assert_called_once_with('secret-value')
        self.assertNotIn('password_hash', result['user'])
