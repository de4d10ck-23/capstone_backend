BEGIN;
-- Apply after 20261028_water_status_history.sql and before deploying the API.
-- Previously discarded selections cannot be reconstructed; leave them unknown.
ALTER TABLE public.water_locations ADD COLUMN IF NOT EXISTS source_type text DEFAULT 'unknown';
COMMENT ON COLUMN public.water_locations.source_type IS 'Registered source type; unknown means it has not been recorded.';

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
    FROM jsonb_each(p_patch) WHERE key IN ('full_name','barangay','latitude','longitude','image_url','source_type');
  proposed := jsonb_populate_record(station, metadata);
  UPDATE public.water_locations SET full_name = proposed.full_name, barangay = proposed.barangay,
    latitude = proposed.latitude, longitude = proposed.longitude, image_url = proposed.image_url,
    source_type = proposed.source_type,
    status = latest.status, e_coli = latest.e_coli, coliform_bacteria = latest.coliform_bacteria,
    bacteriological_exam = latest.bacteriological_exam, sample_date = latest.sample_date,
    sample_time = latest.sample_time, notes = latest.notes, updated_at = now()
    WHERE id = p_location_id RETURNING * INTO station;
  RETURN to_jsonb(station);
END;
$$;
REVOKE ALL ON FUNCTION public.update_water_location_history(uuid,jsonb,date,uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_water_location_history(uuid,jsonb,date,uuid) TO service_role;

NOTIFY pgrst, 'reload schema';
COMMIT;
