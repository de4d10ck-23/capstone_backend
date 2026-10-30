-- Private resident outcomes, written atomically with report status changes.
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS target_user_id UUID REFERENCES public.users(id) ON DELETE CASCADE;
ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS event_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS notifications_event_key_unique ON public.notifications(event_key);
-- Custom JWT authentication is enforced by the API, not Supabase browser sessions.
REVOKE SELECT ON public.notifications FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.notifications TO service_role;

CREATE OR REPLACE FUNCTION public.notify_resident_report_outcome() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE outcome TEXT; kind TEXT;
BEGIN
  IF NEW.status NOT IN ('done','completed','resolved','dismissed','rejected') OR NEW.submitted_by IS NULL THEN
    RETURN NEW;
  END IF;
  outcome := CASE WHEN NEW.status IN ('dismissed','rejected') THEN 'dismissed' ELSE 'done' END;
  kind := CASE WHEN NEW.type = 'unregistered_source' THEN 'Water source submission' ELSE 'Concern' END;
  INSERT INTO public.notifications(title,message,type,barangay,target_roles,target_user_id,created_by,event_key)
  VALUES (kind || ' marked ' || outcome,
    COALESCE(NULLIF(NEW.reason,''), 'Your ' || lower(kind) || ' has been reviewed and marked ' || outcome || '.')
      || ' Submission: ' || COALESCE(NEW.title,kind),
    'report_status', NULL, ARRAY['resident'], NEW.submitted_by, NEW.actioned_by,
    'resident-report:' || NEW.id::text || ':' || outcome)
  ON CONFLICT (event_key) DO NOTHING;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS resident_report_outcome_notification ON public.resident_reports;
CREATE TRIGGER resident_report_outcome_notification AFTER INSERT OR UPDATE OF status ON public.resident_reports
FOR EACH ROW EXECUTE FUNCTION public.notify_resident_report_outcome();
NOTIFY pgrst, 'reload schema';
