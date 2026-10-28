BEGIN;
-- Grandfather existing accounts; only new public sign-ups require proof of email.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS email_verified boolean NOT NULL DEFAULT true;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS auth_version integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.resident_email_tokens (
  token_hash text PRIMARY KEY,
  purpose text NOT NULL CHECK (purpose IN ('register', 'reset')),
  email text NOT NULL,
  rate_key text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  used_at timestamptz
);
CREATE INDEX IF NOT EXISTS resident_email_rate ON public.resident_email_tokens(email, created_at);
CREATE INDEX IF NOT EXISTS resident_ip_rate ON public.resident_email_tokens(rate_key, created_at);
ALTER TABLE public.resident_email_tokens ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.resident_email_tokens FROM anon, authenticated;
GRANT ALL ON public.resident_email_tokens TO service_role;

CREATE OR REPLACE FUNCTION public.issue_resident_email_token(
  p_hash text, p_purpose text, p_email text, p_rate_key text, p_payload jsonb
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  -- Serialize requests across workers, with both mailbox and client limits.
  PERFORM pg_advisory_xact_lock(hashtextextended('email:' || p_email, 0));
  PERFORM pg_advisory_xact_lock(hashtextextended('ip:' || p_rate_key, 0));
  DELETE FROM resident_email_tokens WHERE created_at < now() - interval '1 day';
  IF EXISTS (SELECT 1 FROM resident_email_tokens WHERE email=p_email AND created_at > now()-interval '60 seconds')
    OR (SELECT count(*) FROM resident_email_tokens WHERE email=p_email AND created_at > now()-interval '1 hour') >= 5
    OR (SELECT count(*) FROM resident_email_tokens WHERE rate_key=p_rate_key AND created_at > now()-interval '1 hour') >= 20 THEN
    RETURN false;
  END IF;
  INSERT INTO resident_email_tokens(token_hash,purpose,email,rate_key,payload,expires_at)
  VALUES(p_hash,p_purpose,p_email,p_rate_key,p_payload,now()+interval '15 minutes');
  RETURN true;
END;
$$;

CREATE OR REPLACE FUNCTION public.complete_resident_email_action(p_hash text, p_purpose text, p_password_hash text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE t resident_email_tokens%ROWTYPE; target users%ROWTYPE;
BEGIN
  SELECT * INTO t FROM resident_email_tokens WHERE token_hash=p_hash AND purpose=p_purpose FOR UPDATE;
  IF NOT FOUND OR t.used_at IS NOT NULL OR t.expires_at <= now() THEN RETURN false; END IF;
  IF p_purpose='register' THEN
    PERFORM pg_advisory_xact_lock(hashtextextended('signup:' || t.email, 0));
    IF EXISTS(SELECT 1 FROM users WHERE lower(email)=t.email OR lower(username)=lower(t.payload->>'username')) THEN
      RETURN false;
    END IF;
    INSERT INTO users(full_name,username,email,password_hash,role,barangay,is_active,email_verified)
    VALUES(t.payload->>'full_name',t.payload->>'username',t.email,t.payload->>'password_hash','resident',t.payload->>'barangay',true,true);
  ELSIF p_purpose='reset' THEN
    SELECT * INTO target FROM users WHERE id=(t.payload->>'user_id')::uuid FOR UPDATE;
    IF NOT FOUND OR target.role <> 'resident' OR NOT target.is_active OR NOT target.email_verified
      OR lower(target.email) IS DISTINCT FROM t.email OR p_password_hash IS NULL
      OR target.auth_version <> (t.payload->>'auth_version')::integer THEN RETURN false; END IF;
    UPDATE users SET password_hash=p_password_hash, auth_version=auth_version+1 WHERE id=target.id;
  ELSE RETURN false;
  END IF;
  UPDATE resident_email_tokens SET used_at=now(),payload='{}' WHERE token_hash=p_hash;
  RETURN true;
END;
$$;
REVOKE ALL ON FUNCTION public.issue_resident_email_token(text,text,text,text,jsonb) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.complete_resident_email_action(text,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.issue_resident_email_token(text,text,text,text,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.complete_resident_email_action(text,text,text) TO service_role;
COMMIT;
