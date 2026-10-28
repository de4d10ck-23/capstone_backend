BEGIN;
-- Apply after the structured water status history migration. Keep the original
-- station row, its status logs, linked reports, photos, and saved forecasts.
ALTER TABLE public.water_locations
  ADD COLUMN IF NOT EXISTS archived_at timestamptz,
  ADD COLUMN IF NOT EXISTS archived_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS archived_by_name text;
CREATE INDEX IF NOT EXISTS water_locations_archived_idx
  ON public.water_locations(archived_at, barangay);

CREATE TABLE IF NOT EXISTS public.water_source_archive_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id uuid NOT NULL REFERENCES public.water_locations(id) ON DELETE CASCADE,
  action text NOT NULL CHECK (action IN ('archived', 'restored')),
  actor_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
  actor_name text,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS water_source_archive_events_location_idx
  ON public.water_source_archive_events(location_id, occurred_at);
ALTER TABLE public.water_source_archive_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.water_source_archive_events FROM anon, authenticated;
GRANT ALL ON public.water_source_archive_events TO service_role;

-- An old editor tab cannot modify an archived station or insert status history.
CREATE OR REPLACE FUNCTION public.guard_archived_water_location()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  IF OLD.archived_at IS NOT NULL AND NEW.archived_at IS NOT NULL
     AND NEW IS DISTINCT FROM OLD THEN
    RAISE EXCEPTION 'Restore this water source before editing it';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS guard_archived_water_location ON public.water_locations;
CREATE TRIGGER guard_archived_water_location BEFORE UPDATE ON public.water_locations
FOR EACH ROW EXECUTE FUNCTION public.guard_archived_water_location();

CREATE OR REPLACE FUNCTION public.set_water_source_archived(
  p_location_id uuid, p_actor_id uuid, p_archive boolean
) RETURNS jsonb LANGUAGE plpgsql SET search_path = public AS $$
DECLARE station public.water_locations; actor public.users; stamp timestamptz;
BEGIN
  SELECT * INTO actor FROM public.users WHERE id = p_actor_id AND is_active = true;
  IF actor.id IS NULL OR actor.role NOT IN ('admin', 'sanitization_inspector', 'barangay_official') THEN
    RAISE EXCEPTION 'Only active water source editors can archive or restore stations';
  END IF;
  SELECT * INTO station FROM public.water_locations WHERE id = p_location_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Water source not found'; END IF;
  IF actor.role = 'barangay_official' AND
     (actor.barangay IS NULL OR lower(btrim(actor.barangay)) IS DISTINCT FROM lower(btrim(station.barangay))) THEN
    RAISE EXCEPTION 'You can only manage water sources in your assigned barangay';
  END IF;
  IF p_archive IS NULL THEN RAISE EXCEPTION 'Choose archive or restore'; END IF;
  IF p_archive AND station.archived_at IS NOT NULL THEN RETURN to_jsonb(station); END IF;
  IF NOT p_archive AND station.archived_at IS NULL THEN RETURN to_jsonb(station); END IF;
  stamp := clock_timestamp();
  IF p_archive THEN
    UPDATE public.water_locations SET archived_at = stamp,
      archived_by = actor.id, archived_by_name = actor.full_name, updated_at = stamp
      WHERE id = p_location_id RETURNING * INTO station;
  ELSE
    UPDATE public.water_locations SET archived_at = NULL,
      archived_by = NULL, archived_by_name = NULL, updated_at = stamp
      WHERE id = p_location_id RETURNING * INTO station;
  END IF;
  INSERT INTO public.water_source_archive_events(location_id, action, actor_id, actor_name, occurred_at)
    VALUES (p_location_id, CASE WHEN p_archive THEN 'archived' ELSE 'restored' END,
      actor.id, actor.full_name, stamp);
  RETURN to_jsonb(station);
END;
$$;
REVOKE ALL ON FUNCTION public.set_water_source_archived(uuid,uuid,boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_water_source_archived(uuid,uuid,boolean) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;
