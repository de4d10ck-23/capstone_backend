"""Resident email links. Tokens are random, stored hashed, and consumed atomically."""
import hashlib
import hmac
import secrets
import logging
from html import escape
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from core.config import settings
from core.database import get_supabase

logger = logging.getLogger(__name__)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_email_configuration():
    url = urlparse(settings.RESIDENT_PORTAL_URL)
    if (not settings.RESEND_API_KEY or not settings.RESEND_FROM_EMAIL
            or not url.netloc or url.scheme not in ('http', 'https')
            or (url.scheme == 'http' and url.hostname not in ('localhost', '127.0.0.1'))):
        raise HTTPException(503, 'Email service is not configured. Please contact the administrator.')


async def issue_email_link(email: str, purpose: str, payload: dict, client_ip: str, *, deliver=True):
    check_email_configuration()
    token = secrets.token_urlsafe(32)
    digest = token_hash(token)
    rate_key = hmac.new(settings.JWT_SECRET.encode(), client_ip.encode(), hashlib.sha256).hexdigest()
    sb = get_supabase()
    issued = sb.rpc('issue_resident_email_token', {
        'p_hash': digest, 'p_purpose': purpose, 'p_email': email,
        'p_rate_key': rate_key, 'p_payload': payload,
    }).execute().data
    if not issued:
        if purpose == 'reset':
            return  # Keep account existence private, including rate-limited requests.
        raise HTTPException(429, 'Please wait a minute before requesting another link. Maximum five per hour.')
    if not deliver:
        return
    path = 'verify-email' if purpose == 'register' else 'reset-password'
    label = 'Confirm your resident account' if purpose == 'register' else 'Reset your password'
    # Fragment keeps the secret out of web server access logs and referrer headers.
    link = f'{settings.RESIDENT_PORTAL_URL.rstrip("/")}/{path}#token={token}'
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post('https://api.resend.com/emails', headers={
                'Authorization': f'Bearer {settings.RESEND_API_KEY}',
                'Idempotency-Key': f'resident-{digest}',
            }, json={
                'from': settings.RESEND_FROM_EMAIL, 'to': [email], 'subject': f'WaterWatch: {label}',
                'text': f'{label}: {link}\nThis link expires in 15 minutes and can only be used once. If you did not request this, ignore this email.',
                'html': f'<h2>{label}</h2><p><a href="{escape(link, quote=True)}">{label}</a></p><p>This link expires in 15 minutes and works once. If you did not request this, ignore this email.</p>',
            })
            response.raise_for_status()
    except httpx.HTTPError:
        # Never log API keys, link tokens, or provider response bodies.
        logger.warning('Resend could not send a resident %s email; check sender verification and provider status.', purpose)
        if purpose == 'reset':
            return
        raise HTTPException(503, 'Could not send email. Please wait a minute and try again.') from None


def complete_email_action(token: str, purpose: str, password_hash=None):
    try:
        ok = get_supabase().rpc('complete_resident_email_action', {
            'p_hash': token_hash(token), 'p_purpose': purpose, 'p_password_hash': password_hash,
        }).execute().data
    except Exception as exc:
        if getattr(exc, 'code', None) == '23505':
            raise HTTPException(409, 'That username or email is already registered. Please sign in or register again.') from None
        raise
    if not ok:
        raise HTTPException(400, 'This link is invalid, expired, or already used. Please request a new link.')
