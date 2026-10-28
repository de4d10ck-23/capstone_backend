BEGIN;
-- Apply after 20261028_report_snapshots.sql. Existing archives remain Ready to send.
ALTER TABLE public.reports
  ADD COLUMN IF NOT EXISTS author_name text,
  ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'ready',
  ADD COLUMN IF NOT EXISTS submitted_at timestamptz,
  ADD COLUMN IF NOT EXISTS submitted_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS submitted_by_name text,
  ADD COLUMN IF NOT EXISTS review_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS review_started_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS reviewer_name text,
  ADD COLUMN IF NOT EXISTS reviewed_at timestamptz,
  ADD COLUMN IF NOT EXISTS reviewed_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS reviewed_by_name text,
  ADD COLUMN IF NOT EXISTS review_notes text;
UPDATE public.reports SET author_name=snapshot->>'prepared_by' WHERE author_name IS NULL AND snapshot IS NOT NULL;
ALTER TABLE public.reports DROP CONSTRAINT IF EXISTS reports_review_status_check;
ALTER TABLE public.reports ADD CONSTRAINT reports_review_status_check
  CHECK (review_status IN ('ready','submitted','in_review','done'));
CREATE INDEX IF NOT EXISTS reports_review_queue_idx ON public.reports(review_status, submitted_at);

CREATE TABLE IF NOT EXISTS public.report_review_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  report_id uuid NOT NULL REFERENCES public.reports(id) ON DELETE CASCADE,
  event text NOT NULL CHECK (event IN ('sent','review_started','review_completed')),
  actor_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
  actor_name text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(report_id, event)
);
ALTER TABLE public.report_review_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.report_review_events FROM anon, authenticated;
GRANT ALL ON public.report_review_events TO service_role;

CREATE OR REPLACE FUNCTION public.send_report_to_chu(p_report_id uuid, p_actor_id uuid)
RETURNS jsonb LANGUAGE plpgsql SET search_path = public AS $$
DECLARE r public.reports; actor public.users;
BEGIN
  SELECT * INTO actor FROM public.users WHERE id=p_actor_id AND is_active=true;
  IF actor.id IS NULL OR actor.role NOT IN ('sanitization_inspector','admin') THEN
    RAISE EXCEPTION 'Only active inspectors or administrators can send reports';
  END IF;
  SELECT * INTO r FROM public.reports WHERE id=p_report_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Report not found'; END IF;
  IF actor.role <> 'admin' AND r.generated_by IS DISTINCT FROM p_actor_id THEN
    RAISE EXCEPTION 'Only the report author can send this report';
  END IF;
  IF r.snapshot IS NULL OR r.schema_version IS DISTINCT FROM 1 OR r.content_sha256 IS NULL THEN
    RAISE EXCEPTION 'Generate a report with an archived snapshot before sending';
  END IF;
  -- Retries, double-clicks, and concurrent submissions return the existing receipt.
  IF r.submitted_at IS NOT NULL THEN RETURN to_jsonb(r); END IF;
  IF r.review_status <> 'ready' THEN RAISE EXCEPTION 'This report is not ready to send'; END IF;
  UPDATE public.reports SET review_status='submitted', submitted_at=clock_timestamp(),
    submitted_by=p_actor_id, submitted_by_name=actor.full_name
    WHERE id=p_report_id RETURNING * INTO r;
  INSERT INTO public.report_review_events(report_id,event,actor_id,actor_name,created_at)
    VALUES(r.id,'sent',actor.id,actor.full_name,r.submitted_at);
  INSERT INTO public.notifications(title,message,type,barangay,target_roles,created_by)
    VALUES('Water surveillance report received', r.title || ' is ready for CHU doctor review.',
      'info',r.barangay,ARRAY['city_health_officer'],actor.id);
  RETURN to_jsonb(r);
END;
$$;

CREATE OR REPLACE FUNCTION public.review_archived_report(
  p_report_id uuid, p_actor_id uuid, p_action text, p_notes text DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql SET search_path = public AS $$
DECLARE r public.reports; actor public.users;
BEGIN
  SELECT * INTO actor FROM public.users WHERE id=p_actor_id AND is_active=true;
  IF actor.id IS NULL OR actor.role NOT IN ('city_health_officer','admin') THEN
    RAISE EXCEPTION 'Only an active CHU doctor can review this report';
  END IF;
  IF p_action NOT IN ('start','complete') OR p_action IS NULL THEN RAISE EXCEPTION 'Invalid review action'; END IF;
  SELECT * INTO r FROM public.reports WHERE id=p_report_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Report not found'; END IF;
  IF r.submitted_at IS NULL THEN RAISE EXCEPTION 'The report has not been sent to CHU'; END IF;
  IF r.review_status = 'done' THEN RETURN to_jsonb(r); END IF;
  IF r.review_started_by IS NOT NULL AND r.review_started_by <> actor.id AND actor.role <> 'admin' THEN
    RAISE EXCEPTION 'This report is being reviewed by another doctor';
  END IF;
  IF p_action = 'start' THEN
    IF r.review_status = 'in_review' THEN RETURN to_jsonb(r); END IF;
    IF r.review_status <> 'submitted' THEN RAISE EXCEPTION 'The report is not waiting for review'; END IF;
    UPDATE public.reports SET review_status='in_review', review_started_at=clock_timestamp(),
      review_started_by=actor.id, reviewer_name=actor.full_name
      WHERE id=r.id RETURNING * INTO r;
    INSERT INTO public.report_review_events(report_id,event,actor_id,actor_name,created_at)
      VALUES(r.id,'review_started',actor.id,actor.full_name,r.review_started_at);
  ELSE
    IF r.review_status <> 'in_review' THEN RAISE EXCEPTION 'Start review before marking the report Done'; END IF;
    IF p_notes IS NULL OR length(btrim(p_notes)) NOT BETWEEN 1 AND 5000 THEN
      RAISE EXCEPTION 'Record review notes before marking the report Done';
    END IF;
    UPDATE public.reports SET review_status='done', reviewed_at=clock_timestamp(),
      reviewed_by=actor.id, reviewed_by_name=actor.full_name, review_notes=btrim(p_notes)
      WHERE id=r.id RETURNING * INTO r;
    INSERT INTO public.report_review_events(report_id,event,actor_id,actor_name,notes,created_at)
      VALUES(r.id,'review_completed',actor.id,actor.full_name,r.review_notes,r.reviewed_at);
  END IF;
  RETURN to_jsonb(r);
END;
$$;
REVOKE ALL ON FUNCTION public.send_report_to_chu(uuid,uuid) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.review_archived_report(uuid,uuid,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.send_report_to_chu(uuid,uuid) TO service_role;
GRANT EXECUTE ON FUNCTION public.review_archived_report(uuid,uuid,text,text) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
