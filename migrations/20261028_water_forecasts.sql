-- Saved resident forecasts. Apply in the Supabase SQL editor before deploying.
CREATE TABLE IF NOT EXISTS public.water_forecasts (
    station_id uuid NOT NULL REFERENCES public.water_locations(id) ON DELETE CASCADE,
    forecast_date date NOT NULL,
    prediction jsonb NOT NULL,
    generated_at timestamptz NOT NULL,
    PRIMARY KEY (station_id, forecast_date)
);
CREATE INDEX IF NOT EXISTS water_forecasts_date_idx ON public.water_forecasts(forecast_date);
ALTER TABLE public.water_forecasts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.water_forecasts FROM anon, authenticated;
GRANT ALL ON public.water_forecasts TO service_role;

-- An older overlapping worker must not replace a newer forecast.
CREATE OR REPLACE FUNCTION public.keep_newest_water_forecast()
RETURNS trigger LANGUAGE plpgsql SET search_path = public AS $$
BEGIN
    IF NEW.generated_at < OLD.generated_at THEN RETURN NULL; END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS water_forecasts_keep_newest ON public.water_forecasts;
CREATE TRIGGER water_forecasts_keep_newest BEFORE UPDATE ON public.water_forecasts
FOR EACH ROW EXECUTE FUNCTION public.keep_newest_water_forecast();
