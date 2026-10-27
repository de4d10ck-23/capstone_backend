-- Run once in the connected Supabase project's SQL Editor.
-- Shared per-account read receipts; existing users and notifications are preserved.
CREATE TABLE IF NOT EXISTS public.notification_reads (
    user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    notification_id UUID NOT NULL REFERENCES public.notifications(id) ON DELETE CASCADE,
    read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, notification_id)
);
CREATE INDEX IF NOT EXISTS idx_notification_reads_user ON public.notification_reads(user_id);
-- The API uses the service role; browser clients must not access other users' receipts.
ALTER TABLE public.notification_reads ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.notification_reads TO service_role;
NOTIFY pgrst, 'reload schema';
