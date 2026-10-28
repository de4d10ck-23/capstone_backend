-- Apply in Supabase SQL Editor before deploying the report-workflow changes.
BEGIN;
ALTER TABLE public.resident_reports DROP CONSTRAINT IF EXISTS resident_reports_status_check;
ALTER TABLE public.resident_reports ADD CONSTRAINT resident_reports_status_check
  CHECK (status IN ('pending','validated','rejected','escalated','passed_to_chu','assigned','in_progress','processing','completed','resolved','done','dismissed'));
ALTER TABLE public.inspection_requests DROP CONSTRAINT IF EXISTS inspection_requests_status_check;
ALTER TABLE public.inspection_requests ADD CONSTRAINT inspection_requests_status_check
  CHECK (status IN ('pending','assigned','in_progress','completed','cancelled'));
ALTER TABLE public.inspection_requests ADD COLUMN IF NOT EXISTS resident_report_id UUID
  REFERENCES public.resident_reports(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS inspection_requests_resident_report_unique
  ON public.inspection_requests(resident_report_id) WHERE resident_report_id IS NOT NULL;

-- Recover legacy links only when both sides have exactly one exact-content match.
WITH candidates AS (
  SELECT i.id AS inspection_id, r.id AS report_id,
    COUNT(*) OVER (PARTITION BY i.id) AS report_matches,
    COUNT(*) OVER (PARTITION BY r.id) AS inspection_matches
  FROM public.inspection_requests i JOIN public.resident_reports r
    ON i.requested_by IS NOT DISTINCT FROM r.submitted_by
    AND i.barangay IS NOT DISTINCT FROM r.barangay
    AND POSITION('passed to CHU by Brgy Official] ' || r.title || ': ' || r.description IN i.description) > 0
  WHERE i.resident_report_id IS NULL
    AND NOT EXISTS (SELECT 1 FROM public.inspection_requests linked WHERE linked.resident_report_id = r.id)
)
UPDATE public.inspection_requests i SET resident_report_id = c.report_id
FROM candidates c WHERE i.id = c.inspection_id AND c.report_matches = 1 AND c.inspection_matches = 1;

-- Explicit statuses replace the old rejected-with-a-marker workaround.
UPDATE public.resident_reports SET status = 'done', reason = TRIM(REPLACE(reason,'[DONE]',''))
  WHERE status = 'rejected' AND reason LIKE '[DONE]%';
UPDATE public.resident_reports SET status = 'dismissed', reason = TRIM(REPLACE(reason,'[DISMISSED]',''))
  WHERE status = 'rejected' AND reason LIKE '[DISMISSED]%';

CREATE OR REPLACE FUNCTION public.sync_inspection_report_status() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  IF pg_trigger_depth() > 1 OR NEW.resident_report_id IS NULL THEN RETURN NEW; END IF;
  UPDATE public.resident_reports SET
    status = CASE NEW.status WHEN 'completed' THEN 'done' WHEN 'cancelled' THEN 'dismissed'
      WHEN 'in_progress' THEN 'in_progress' ELSE 'escalated' END,
    reason = COALESCE(NULLIF(NEW.notes,''), reason),
    actioned_by = COALESCE(NEW.assigned_to, actioned_by)
  WHERE id = NEW.resident_report_id;
  RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.sync_report_inspection_status() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  IF pg_trigger_depth() > 1 THEN RETURN NEW; END IF;
  IF NEW.status IN ('done','completed','resolved','in_progress','processing','dismissed','rejected') THEN
    UPDATE public.inspection_requests SET
      status = CASE WHEN NEW.status IN ('done','completed','resolved') THEN 'completed'
        WHEN NEW.status IN ('dismissed','rejected') THEN 'cancelled' ELSE 'in_progress' END,
      notes = COALESCE(NULLIF(NEW.reason,''), notes)
    WHERE resident_report_id = NEW.id;
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS inspection_report_status_sync ON public.inspection_requests;
CREATE TRIGGER inspection_report_status_sync AFTER INSERT OR UPDATE OF status, notes ON public.inspection_requests
  FOR EACH ROW EXECUTE FUNCTION public.sync_inspection_report_status();
DROP TRIGGER IF EXISTS report_inspection_status_sync ON public.resident_reports;
CREATE TRIGGER report_inspection_status_sync AFTER UPDATE OF status, reason ON public.resident_reports
  FOR EACH ROW EXECUTE FUNCTION public.sync_report_inspection_status();

-- Repair linked historical completions without reverting a completed report to pending.
UPDATE public.inspection_requests i SET status = 'cancelled'
FROM public.resident_reports r WHERE i.resident_report_id = r.id AND r.status IN ('dismissed','rejected');
UPDATE public.inspection_requests i SET status = 'completed'
FROM public.resident_reports r WHERE i.resident_report_id = r.id AND r.status IN ('done','completed','resolved');
UPDATE public.resident_reports r SET status = CASE WHEN i.status='completed' THEN 'done' WHEN i.status='assigned' THEN 'escalated' ELSE 'in_progress' END,
  reason = COALESCE(NULLIF(i.notes,''), r.reason)
FROM public.inspection_requests i WHERE i.resident_report_id=r.id
  AND i.status IN ('completed','in_progress','assigned') AND r.status NOT IN ('done','completed','resolved','dismissed','rejected');

-- Lock the report so repeat clicks/concurrent forwarding cannot create duplicate tickets.
CREATE OR REPLACE FUNCTION public.forward_resident_report_to_chu(p_report_id UUID, p_actor_id UUID, p_reason TEXT DEFAULT NULL)
RETURNS JSONB LANGUAGE plpgsql SET search_path = public AS $$
DECLARE r public.resident_reports; ticket_id UUID; source_id UUID; matched_id TEXT;
BEGIN
  SELECT * INTO r FROM public.resident_reports WHERE id=p_report_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Report not found'; END IF;
  IF r.status IN ('done','completed','resolved','dismissed','rejected') THEN
    RAISE EXCEPTION 'Closed reports cannot be forwarded again';
  END IF;
  SELECT id INTO ticket_id FROM public.inspection_requests WHERE resident_report_id=r.id;
  IF ticket_id IS NULL THEN
    matched_id := substring(r.description from '\(ID:\s*([a-fA-F0-9-]{36})\)');
    IF matched_id IS NOT NULL THEN
      SELECT id INTO source_id FROM public.water_locations WHERE id=matched_id::uuid;
    END IF;
    INSERT INTO public.inspection_requests
      (resident_report_id, location_id, description, barangay, latitude, longitude, requested_by, status)
    VALUES (r.id,source_id,r.description,r.barangay,r.latitude,r.longitude,r.submitted_by,'pending')
    RETURNING id INTO ticket_id;
  END IF;
  UPDATE public.resident_reports SET
    status=CASE WHEN r.status IN ('in_progress','processing') THEN 'in_progress' ELSE 'escalated' END,
    actioned_by=p_actor_id, reason=COALESCE(p_reason,reason) WHERE id=r.id;
  SELECT * INTO r FROM public.resident_reports WHERE id=p_report_id;
  RETURN to_jsonb(r);
END $$;
REVOKE ALL ON FUNCTION public.forward_resident_report_to_chu(UUID,UUID,TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.forward_resident_report_to_chu(UUID,UUID,TEXT) TO service_role;
-- Recover reports whose old forwarding handler failed before creating a ticket.
DO $$
DECLARE report_row public.resident_reports;
BEGIN
  FOR report_row IN SELECT r.* FROM public.resident_reports r
    WHERE r.status IN ('escalated','passed_to_chu','in_progress','processing')
    AND NOT EXISTS (SELECT 1 FROM public.inspection_requests i WHERE i.resident_report_id=r.id)
    AND NOT EXISTS (SELECT 1 FROM public.inspection_requests i WHERE
      i.requested_by IS NOT DISTINCT FROM r.submitted_by AND i.barangay IS NOT DISTINCT FROM r.barangay
      AND POSITION('passed to CHU by Brgy Official] ' || r.title || ': ' || r.description IN i.description)>0)
  LOOP
    PERFORM public.forward_resident_report_to_chu(report_row.id,report_row.actioned_by,report_row.reason);
  END LOOP;
END $$;
NOTIFY pgrst, 'reload schema';
COMMIT;

-- Review ambiguous legacy matches before linking manually (no automatic title-only matching).
-- SELECT id, description FROM public.inspection_requests
-- WHERE resident_report_id IS NULL AND description LIKE '%passed to CHU by Brgy Official]%';
