BEGIN;
-- Generated reports archive their inputs; downloads never substitute newer live data.
ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS snapshot jsonb;
ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS content_sha256 text;
ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS schema_version integer;
ALTER TABLE public.reports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.reports FROM anon, authenticated;
GRANT ALL ON public.reports TO service_role;
COMMENT ON COLUMN public.reports.snapshot IS 'Versioned water surveillance report snapshot. Legacy reports have NULL and must be regenerated.';
NOTIFY pgrst, 'reload schema';
COMMIT;
