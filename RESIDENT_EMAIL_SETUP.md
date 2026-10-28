# Resident verification and password reset

Public registration now stores a pending request, not a user account. A resident must open the verification email and press **Verify email and create account** within 15 minutes. Admin-created accounts bypass email verification. Existing accounts retain access. Resident login includes **Forgot password?**; resetting a password invalidates existing sessions. Admin/staff and inactive accounts cannot use this reset flow.

## Before deployment

1. In [Resend Domains](https://resend.com/domains), add a domain you own, such as `tortor.org` (or a dedicated sending subdomain). Add the DNS records Resend provides to your DNS host and wait for Verified status. Your Gmail login is not a verified sending domain. The test sender `onboarding@resend.dev` can send only to the email on your Resend account.
2. Set backend environment variables:
   - `RESEND_API_KEY`: your Resend sending API key. The supplied key is saved only in local `backend/.env`, which is ignored; add it separately to your deployed backend's secret settings.
   - `RESEND_FROM_EMAIL`: for example `WaterWatch <noreply@tortor.org>`, after verifying `tortor.org`. It is intentionally blank locally until the sender is ready.
   - `RESIDENT_PORTAL_URL=https://waterwatch.tortor.org` (no `/login` suffix). Local development can use `http://localhost:5982`.
3. Apply `migrations/20261029_resident_email_auth.sql` in your Supabase SQL Editor before deploying the new backend. The service-role key cannot apply schema migrations through the normal table API. This migration has been validated in an isolated PostgreSQL-compatible database, not applied to the hosted database.
4. Install backend requirements, deploy/restart the backend, and deploy the resident frontend. Configure your static host to serve the SPA for `/verify-email`, `/forgot-password`, and `/reset-password`, as it does for `/login`.
5. Disable Resend click/open tracking for auth emails. Links carry a token in the URL fragment; the page clears it from the address bar and consumes it only on explicit submission, so email scanners opening the link do not create accounts or reset passwords.
6. Verify delivery using a resident email you control: sign up, confirm, log in, request a reset, change the password, and check that a previously signed-in session is rejected. No live messages were sent during implementation.

References: [Resend send-email API](https://resend.com/docs/api-reference/emails/send-email), [verified domains](https://resend.com/docs/dashboard/domains/introduction), [test sender limits](https://resend.com/docs/knowledge-base/403-error-resend-dev-domain).

## Operational details

Links are random 256-bit secrets, stored only as SHA-256 hashes. Purpose, expiry, one-time consumption and account creation/password updates are enforced in one database transaction. Pending registration passwords are bcrypt hashes. Successful confirmation clears the pending payload; expired records are pruned after one day when new requests arrive. Token tables and functions are restricted to the service role.

Requests allow one link per minute and five per hour per email, plus twenty per hour per client IP, enforced across backend workers. Configure your reverse proxy/ASGI server to trust forwarded client IPs only from your actual proxy; otherwise all users may share a proxy limit, or untrusted headers may bypass it. Forgot-password responses deliberately do not reveal whether an account exists, is eligible, is rate limited, or encountered a delivery error. Delivery failures produce a backend warning without API keys, tokens or provider response bodies.

Verification does not automatically log in a resident. Existing accounts are grandfathered because the database does not reliably distinguish historical public sign-ups from admin-created accounts. Admin accounts still use the existing admin account-creation form; email verification is bypassed, not the form's email field.

Checks: `tests/test_resident_email_auth.py`, `tests/test_resident_profile.py`, and `tests/check_resident_email_auth.mjs` (pass the installed PGlite `dist/index.js` path). Run `npm.cmd run build` in `frontend/resident`.
