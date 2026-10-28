-- Structured, append-only day snapshots. Apply before deploying the history changes.
ALTER TABLE public.water_status_logs
  ADD COLUMN IF NOT EXISTS effective_date date,
  ADD COLUMN IF NOT EXISTS sample_date date,
  ADD COLUMN IF NOT EXISTS sample_time time,
  ADD COLUMN IF NOT EXISTS e_coli boolean,
  ADD COLUMN IF NOT EXISTS coliform_bacteria boolean;

-- Legacy recorded_at represented the sample's Philippine local timestamp.
UPDATE public.water_status_logs SET
  effective_date = COALESCE(effective_date, (recorded_at AT TIME ZONE 'Asia/Manila')::date),
  sample_date = COALESCE(sample_date, (recorded_at AT TIME ZONE 'Asia/Manila')::date),
  sample_time = COALESCE(sample_time, (recorded_at AT TIME ZONE 'Asia/Manila')::time),
  e_coli = CASE WHEN status IN ('unknown', 'pending') THEN NULL ELSE COALESCE(e_coli, e_coli_count > 0, false) END,
  coliform_bacteria = CASE WHEN status IN ('unknown', 'pending') THEN NULL ELSE COALESCE(coliform_bacteria, coliform_count > 0, false) END
WHERE effective_date IS NULL;

INSERT INTO public.water_status_logs
  (location_id, status, bacteriological_exam, e_coli, coliform_bacteria,
   e_coli_count, coliform_count, notes, effective_date, sample_date, sample_time, recorded_by)
SELECT w.id, COALESCE(w.status, 'unknown'), w.bacteriological_exam, w.e_coli, w.coliform_bacteria,
  CASE WHEN w.e_coli THEN 1 ELSE 0 END, CASE WHEN w.coliform_bacteria THEN 1 ELSE 0 END,
  w.notes, COALESCE(w.sample_date, (w.created_at AT TIME ZONE 'Asia/Manila')::date,
    (now() AT TIME ZONE 'Asia/Manila')::date), w.sample_date, w.sample_time, w.created_by
FROM public.water_locations w
WHERE NOT EXISTS (SELECT 1 FROM public.water_status_logs l WHERE l.location_id = w.id);

ALTER TABLE public.water_status_logs ALTER COLUMN effective_date SET NOT NULL;
CREATE INDEX IF NOT EXISTS water_status_logs_effective_idx
  ON public.water_status_logs(location_id, effective_date DESC, recorded_at DESC, id DESC);
ALTER TABLE public.water_status_logs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.water_status_logs FROM anon, authenticated;
GRANT ALL ON public.water_status_logs TO service_role;

-- Preserve revisions within a day. Only the latest revision determines its displayed snapshot.
CREATE OR REPLACE FUNCTION public.update_water_location_history(
  p_location_id uuid, p_patch jsonb, p_effective_date date, p_actor uuid
) RETURNS jsonb LANGUAGE plpgsql SET search_path = public AS $$
DECLARE
  station public.water_locations;
  prior public.water_status_logs;
  latest public.water_status_logs;
  proposed public.water_locations;
  before_snapshot jsonb;
  after_snapshot jsonb;
  metadata jsonb;
BEGIN
  IF p_effective_date IS NULL OR p_effective_date > (now() AT TIME ZONE 'Asia/Manila')::date THEN
    RAISE EXCEPTION 'Choose today or an earlier history date';
  END IF;
  SELECT * INTO STRICT station FROM public.water_locations WHERE id = p_location_id FOR UPDATE;
  SELECT * INTO prior FROM public.water_status_logs WHERE location_id = p_location_id
    AND effective_date <= p_effective_date ORDER BY effective_date DESC, recorded_at DESC, id DESC LIMIT 1;
  before_snapshot := jsonb_build_object('status', COALESCE(prior.status, 'unknown'),
    'e_coli', prior.e_coli, 'coliform_bacteria', prior.coliform_bacteria,
    'bacteriological_exam', COALESCE(prior.bacteriological_exam, 'untested'),
    'sample_date', prior.sample_date, 'sample_time', prior.sample_time, 'notes', COALESCE(prior.notes, ''));
  -- Only supplied history fields override the snapshot that applied on the selected day.
  SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) INTO after_snapshot
    FROM jsonb_each(p_patch) WHERE key IN ('status','e_coli','coliform_bacteria',
      'bacteriological_exam','sample_date','sample_time','notes');
  after_snapshot := before_snapshot || after_snapshot;
  IF after_snapshot->>'status' = 'undrinkable' THEN
    after_snapshot := after_snapshot || '{"status":"contaminated"}'::jsonb;
  END IF;
  IF after_snapshot->>'status' NOT IN ('safe','warning','contaminated','unknown','pending') THEN
    RAISE EXCEPTION 'Invalid safety status';
  END IF;
  IF after_snapshot->>'status' IN ('unknown','pending') THEN
    after_snapshot := after_snapshot || '{"e_coli":null,"coliform_bacteria":null,"bacteriological_exam":"untested"}'::jsonb;
  END IF;
  proposed := jsonb_populate_record(NULL::public.water_locations, after_snapshot);
  IF proposed.sample_date > p_effective_date THEN
    RAISE EXCEPTION 'The sample date cannot be later than the selected history day';
  END IF;
  IF after_snapshot IS DISTINCT FROM before_snapshot THEN
    INSERT INTO public.water_status_logs(location_id, effective_date, status, previous_status,
      e_coli, coliform_bacteria, e_coli_count, coliform_count, bacteriological_exam,
      sample_date, sample_time, notes, recorded_by, recorded_at)
    VALUES (p_location_id, p_effective_date, proposed.status, prior.status,
      proposed.e_coli, proposed.coliform_bacteria, CASE WHEN proposed.e_coli THEN 1 ELSE 0 END,
      CASE WHEN proposed.coliform_bacteria THEN 1 ELSE 0 END, proposed.bacteriological_exam,
      proposed.sample_date, proposed.sample_time, proposed.notes, p_actor, clock_timestamp());
  END IF;
  SELECT * INTO latest FROM public.water_status_logs WHERE location_id = p_location_id
    ORDER BY effective_date DESC, recorded_at DESC, id DESC LIMIT 1;
  SELECT COALESCE(jsonb_object_agg(key, value), '{}'::jsonb) INTO metadata
    FROM jsonb_each(p_patch) WHERE key IN ('full_name','barangay','latitude','longitude','image_url');
  proposed := jsonb_populate_record(station, metadata);
  UPDATE public.water_locations SET full_name = proposed.full_name, barangay = proposed.barangay,
    latitude = proposed.latitude, longitude = proposed.longitude, image_url = proposed.image_url,
    status = latest.status, e_coli = latest.e_coli, coliform_bacteria = latest.coliform_bacteria,
    bacteriological_exam = latest.bacteriological_exam, sample_date = latest.sample_date,
    sample_time = latest.sample_time, notes = latest.notes, updated_at = now()
    WHERE id = p_location_id RETURNING * INTO station;
  RETURN to_jsonb(station);
END;
$$;
REVOKE ALL ON FUNCTION public.update_water_location_history(uuid,jsonb,date,uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_water_location_history(uuid,jsonb,date,uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.register_water_location_history()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
  INSERT INTO public.water_status_logs(location_id, effective_date, status, e_coli, coliform_bacteria,
    e_coli_count, coliform_count, bacteriological_exam, sample_date, sample_time, notes, recorded_by)
  VALUES (NEW.id, COALESCE(NEW.sample_date, (now() AT TIME ZONE 'Asia/Manila')::date),
    COALESCE(NEW.status, 'unknown'), NEW.e_coli, NEW.coliform_bacteria,
    CASE WHEN NEW.e_coli THEN 1 ELSE 0 END, CASE WHEN NEW.coliform_bacteria THEN 1 ELSE 0 END,
    NEW.bacteriological_exam, NEW.sample_date, NEW.sample_time, NEW.notes, NEW.created_by);
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS water_location_registration_history ON public.water_locations;
CREATE TRIGGER water_location_registration_history AFTER INSERT ON public.water_locations
FOR EACH ROW EXECUTE FUNCTION public.register_water_location_history();
