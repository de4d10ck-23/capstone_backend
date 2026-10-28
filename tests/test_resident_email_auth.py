"""Auth boundary tests with no database writes or real email delivery."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import auth, users
from core.dependencies import get_current_user, get_optional_user, require_admin
from services import email_auth_service as mail


class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(auth.router)
        self.app.include_router(users.router)
        self.client = TestClient(self.app)
        self.db = Mock()
        self.db.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        self.db.table.return_value.select.return_value.ilike.return_value.execute.return_value.data = []
        self.form = dict(full_name='New Resident', username='newresident', email='RESIDENT@example.com', password='secret123', barangay='Abgao')

    def test_signup_sends_link_without_creating_user(self):
        with patch.object(auth, 'get_supabase', return_value=self.db), patch.object(auth, 'issue_email_link', new_callable=AsyncMock) as send:
            response = self.client.post('/api/auth/register', json=self.form)
        self.assertEqual(response.status_code, 200)
        self.db.table.return_value.insert.assert_not_called()
        email, purpose, pending, _ = send.call_args.args
        self.assertEqual((email,purpose), ('resident@example.com','register'))
        self.assertNotIn('password',pending)
        self.assertNotEqual(pending['password_hash'],self.form['password'])
        self.assertNotIn('user',response.json())

    def test_signup_requires_email_and_rejects_role_override(self):
        for changes in ({'email':None},{'email':'bad'},{'role':'admin'},{'email_verified':True},{'username':'   '}):
            self.assertEqual(self.client.post('/api/auth/register',json={**self.form,**changes}).status_code,422)

    def test_verification_does_not_log_user_in(self):
        with patch.object(auth,'complete_email_action') as complete:
            response=self.client.post('/api/auth/verify-email',json={'token':'x'*43})
        self.assertEqual(response.status_code,200)
        complete.assert_called_once_with('x'*43,'register')
        self.assertNotIn('token',response.json())

    def test_login_blocks_unverified_account(self):
        self.db.table.return_value.select.return_value.eq.return_value.execute.return_value.data=[
            {'id':'pending','password_hash':'hash','is_active':True,'email_verified':False}]
        with patch.object(auth,'get_supabase',return_value=self.db),patch.object(auth,'verify_password',return_value=True):
            response=self.client.post('/api/auth/login',json={'username':'pending','password':'secret'})
        self.assertEqual(response.status_code,403)
        self.assertNotIn('token',response.json())

    def test_reset_requests_hide_account_existence_and_limit_to_residents(self):
        outputs=[]
        for row in (None,dict(id='1',role='admin',is_active=True),dict(id='2',role='resident',is_active=False),dict(id='3',role='resident',is_active=True,email_verified=False),dict(id='4',role='resident',is_active=True,email_verified=True,auth_version=2)):
            self.db.table.return_value.select.return_value.ilike.return_value.execute.return_value.data=[row] if row else []
            with patch.object(auth,'get_supabase',return_value=self.db),patch.object(auth,'issue_email_link',new_callable=AsyncMock) as send:
                response=self.client.post('/api/auth/forgot-password',json={'email':'resident@example.com'})
            outputs.append(response.json())
            self.assertEqual(send.call_args.kwargs['deliver'], bool(row and row['id']=='4'))
        self.assertTrue(all(x==outputs[0] for x in outputs))

    def test_reset_hashes_password_and_uses_reset_purpose(self):
        with patch.object(auth,'complete_email_action') as complete:
            response=self.client.post('/api/auth/reset-password',json={'token':'x'*43,'password':'newsecret'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(complete.call_args.args[:2],('x'*43,'reset'))
        self.assertNotEqual(complete.call_args.args[2],'newsecret')
        for password in ('short','      ','é'*40):
            self.assertIn(self.client.post('/api/auth/reset-password',json={'token':'x'*43,'password':password}).status_code,(400,422))

    def test_admin_creation_is_verified_without_mail(self):
        self.app.dependency_overrides[require_admin]=lambda: {'id':'admin','role':'admin'}
        self.db.table.return_value.insert.return_value.execute.return_value.data=[{'id':'new','role':'resident','email_verified':True}]
        with patch.object(users,'get_supabase',return_value=self.db),patch.object(auth,'issue_email_link',new_callable=AsyncMock) as send:
            response=self.client.post('/api/users',json={**self.form,'role':'resident'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(self.db.table.return_value.insert.call_args.args[0]['email_verified'])
        send.assert_not_called()

    def test_old_sessions_rejected_after_reset(self):
        user={'id':'1','is_active':True,'email_verified':True,'auth_version':1}
        self.db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data=[user]
        self.db.table.return_value.select.return_value.eq.return_value.single.return_value.execute.return_value.data=user
        for version in (None,0,1):
            payload={'sub':'1'}
            if version is not None: payload['auth_version']=version
            with patch('core.dependencies.get_supabase',return_value=self.db),patch('core.dependencies.decode_access_token',return_value=payload):
                credentials=SimpleNamespace(credentials='signed-token')
                if version==1:
                    self.assertEqual(asyncio.run(get_current_user(credentials)),user)
                else:
                    with self.assertRaises(HTTPException): asyncio.run(get_current_user(credentials))
                    self.assertIsNone(asyncio.run(get_optional_user(credentials)))


class ResendTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_never_sends_email(self):
        db=Mock(); db.rpc.return_value.execute.return_value.data=False
        config=SimpleNamespace(RESEND_API_KEY='test-key',RESEND_FROM_EMAIL='a@example.com',RESIDENT_PORTAL_URL='https://example.com',JWT_SECRET='secret')
        with patch.object(mail,'settings',config),patch.object(mail,'get_supabase',return_value=db),patch.object(mail.httpx,'AsyncClient') as client:
            with self.assertRaises(HTTPException) as err:
                await mail.issue_email_link('a@example.com','register',{},'ip')
            self.assertEqual(err.exception.status_code,429)
            await mail.issue_email_link('a@example.com','reset',{},'ip')
            client.assert_not_called()

    async def test_resend_payload_and_hashed_storage(self):
        db=Mock()
        db.rpc.return_value.execute.return_value.data=True
        http=AsyncMock()
        http.post.return_value=Mock()
        context=AsyncMock()
        context.__aenter__.return_value=http
        config=SimpleNamespace(RESEND_API_KEY='test-key',RESEND_FROM_EMAIL='WaterWatch <noreply@example.com>',RESIDENT_PORTAL_URL='https://waterwatch.example.com',JWT_SECRET='test-secret')
        with patch.object(mail,'settings',config),patch.object(mail,'get_supabase',return_value=db),patch.object(mail.httpx,'AsyncClient',return_value=context):
            await mail.issue_email_link('resident@example.com','register',{'password_hash':'hash'},'127.0.0.1')
        stored=db.rpc.call_args.args[1]
        sent=http.post.call_args.kwargs
        self.assertEqual(len(stored['p_hash']),64)
        self.assertNotEqual(stored['p_rate_key'],'127.0.0.1')
        self.assertEqual(sent['json']['to'],['resident@example.com'])
        self.assertIn('/verify-email#token=',sent['json']['text'])
        self.assertNotIn('test-key',sent['json']['text'])
        self.assertEqual(sent['headers']['Authorization'],'Bearer test-key')
        self.assertNotIn('password_hash',sent['json']['text'])

    async def test_provider_failure_is_safe(self):
        import httpx
        db=Mock(); db.rpc.return_value.execute.return_value.data=True
        http=AsyncMock(); http.post.side_effect=httpx.ConnectError('private provider detail')
        context=AsyncMock(); context.__aenter__.return_value=http
        config=SimpleNamespace(RESEND_API_KEY='test-key',RESEND_FROM_EMAIL='a@example.com',RESIDENT_PORTAL_URL='https://example.com',JWT_SECRET='secret')
        with patch.object(mail,'settings',config),patch.object(mail,'get_supabase',return_value=db),patch.object(mail.httpx,'AsyncClient',return_value=context):
            with self.assertRaises(HTTPException) as err:
                await mail.issue_email_link('a@example.com','register',{},'ip')
            self.assertEqual(err.exception.status_code,503)
            self.assertNotIn('private',err.exception.detail)
            await mail.issue_email_link('a@example.com','reset',{},'ip')


if __name__=='__main__': unittest.main()
