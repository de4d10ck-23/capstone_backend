import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_push_delivery import load_functions
from test_notification_reads import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from models.user import ResidentProfileUpdate
from pydantic import ValidationError


class ResidentProfileTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.table.return_value.select.return_value.ilike.return_value.neq.return_value.execute.return_value.data = []
        self.db.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [
            {'id': 'resident', 'full_name': 'New Name', 'username': 'newname', 'password_hash': 'secret', 'barangay': 'Combado'}]
        self.hash = Mock(return_value='new-hash')
        self.update = load_functions('routers/auth.py', {'update_resident_profile'},
            get_supabase=lambda: self.db, HTTPException=HTTPException,
            hash_password=self.hash, verify_password=lambda password, hashed: password == 'correct')['update_resident_profile']
        self.actor = {'id': 'resident', 'role': 'resident', 'password_hash': 'old-hash'}

    def body(self, **kwargs):
        return ResidentProfileUpdate(full_name=' New Name ', username=' newname ', **kwargs)

    def test_only_authenticated_account_updated_and_hash_hidden(self):
        result = asyncio.run(self.update(self.body(), self.actor))
        self.db.table.return_value.update.assert_called_once_with({'full_name': 'New Name', 'username': 'newname'})
        self.db.table.return_value.update.return_value.eq.assert_called_once_with('id', 'resident')
        self.assertNotIn('password_hash', result['user'])
        self.hash.assert_not_called()

    def test_password_change_requires_current_password(self):
        for password in (None, 'wrong'):
            with self.assertRaises(HTTPException):
                asyncio.run(self.update(self.body(new_password='new-password', current_password=password), self.actor))
        self.db.table.return_value.update.assert_not_called()

    def test_password_is_hashed(self):
        asyncio.run(self.update(self.body(new_password='new-password', current_password='correct'), self.actor))
        self.hash.assert_called_once_with('new-password')
        self.assertEqual(self.db.table.return_value.update.call_args.args[0]['password_hash'], 'new-hash')

    def test_duplicate_username_does_not_update(self):
        self.db.table.return_value.select.return_value.ilike.return_value.neq.return_value.execute.return_value.data = [{'id': 'other'}]
        with self.assertRaises(HTTPException) as err:
            asyncio.run(self.update(self.body(), self.actor))
        self.assertEqual(err.exception.status_code, 409)
        self.db.table.return_value.update.assert_not_called()

    def test_forbidden_fields_and_blank_names_rejected(self):
        for extra in ({'barangay': 'Abgao'}, {'role': 'admin'}, {'id': 'other'}):
            with self.assertRaises(ValidationError):
                self.body(**extra)
        for field in ('full_name', 'username'):
            values = {'full_name': 'Name', 'username': 'name', field: '   '}
            with self.assertRaises(ValidationError):
                ResidentProfileUpdate(**values)

    def test_nonresident_rejected(self):
        with self.assertRaises(HTTPException) as err:
            asyncio.run(self.update(self.body(), {**self.actor, 'role': 'admin'}))
        self.assertEqual(err.exception.status_code, 403)
        self.db.table.return_value.update.assert_not_called()
