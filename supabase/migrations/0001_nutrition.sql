-- Riva Snap backend, migration 0001: nutrition subset + scan logging.
--
-- Tables profiles, health_goals, nutrition_goals, and nutrition_days are
-- taken verbatim from FINAL_DATABASE_SCHEMA.md (with IF NOT EXISTS guards so
-- the full schema can be applied later without conflict). food_entries and
-- log_scan() are new: they persist accepted scans and increment the daily
-- aggregate in one transaction.

-- ---------------------------------------------------------------------------
-- Utility
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- profiles
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.profiles (
  id                uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  name              text NOT NULL DEFAULT 'there',
  date_of_birth     date,
  gender            text CHECK (gender IN ('female', 'male', 'non-binary', 'prefer-not-to-say')),
  clinician_name    text,
  start_weight      numeric(6,2) CHECK (start_weight > 0 OR start_weight IS NULL),
  goal_weight       numeric(6,2) CHECK (goal_weight > 0 OR goal_weight IS NULL),
  height_inches     numeric(5,2) CHECK (height_inches > 0 OR height_inches IS NULL),
  timezone          text NOT NULL DEFAULT 'America/New_York',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON public.profiles;
CREATE TRIGGER trg_profiles_updated_at
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_profiles_dob ON public.profiles(date_of_birth)
  WHERE date_of_birth IS NOT NULL;

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS profiles_select ON public.profiles;
CREATE POLICY profiles_select ON public.profiles
  FOR SELECT TO authenticated
  USING (id = auth.uid());

DROP POLICY IF EXISTS profiles_insert ON public.profiles;
CREATE POLICY profiles_insert ON public.profiles
  FOR INSERT TO authenticated
  WITH CHECK (id = auth.uid());

DROP POLICY IF EXISTS profiles_update ON public.profiles;
CREATE POLICY profiles_update ON public.profiles
  FOR UPDATE TO authenticated
  USING (id = auth.uid())
  WITH CHECK (id = auth.uid());

-- ---------------------------------------------------------------------------
-- health_goals (needed by the signup trigger)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.health_goals (
  user_id         uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  glp1_support    boolean NOT NULL DEFAULT false,
  weight_mgmt     boolean NOT NULL DEFAULT false,
  nutrition_diet  boolean NOT NULL DEFAULT false,
  muscle_preserve boolean NOT NULL DEFAULT false,
  exercise_move   boolean NOT NULL DEFAULT false,
  sleep_recovery  boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_health_goals_updated_at ON public.health_goals;
CREATE TRIGGER trg_health_goals_updated_at
  BEFORE UPDATE ON public.health_goals
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.health_goals ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS health_goals_select ON public.health_goals;
CREATE POLICY health_goals_select ON public.health_goals
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS health_goals_insert ON public.health_goals;
CREATE POLICY health_goals_insert ON public.health_goals
  FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS health_goals_update ON public.health_goals;
CREATE POLICY health_goals_update ON public.health_goals
  FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- nutrition_goals
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.nutrition_goals (
  user_id       uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  protein_goal  integer NOT NULL DEFAULT 130 CHECK (protein_goal >= 0),
  carb_goal     integer NOT NULL DEFAULT 284 CHECK (carb_goal >= 0),
  fiber_goal    integer NOT NULL DEFAULT 30 CHECK (fiber_goal >= 0),
  water_goal    integer NOT NULL DEFAULT 80 CHECK (water_goal >= 0),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_nutrition_goals_updated_at ON public.nutrition_goals;
CREATE TRIGGER trg_nutrition_goals_updated_at
  BEFORE UPDATE ON public.nutrition_goals
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

ALTER TABLE public.nutrition_goals ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nutrition_goals_select ON public.nutrition_goals;
CREATE POLICY nutrition_goals_select ON public.nutrition_goals
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS nutrition_goals_insert ON public.nutrition_goals;
CREATE POLICY nutrition_goals_insert ON public.nutrition_goals
  FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS nutrition_goals_update ON public.nutrition_goals;
CREATE POLICY nutrition_goals_update ON public.nutrition_goals
  FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- nutrition_days
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.nutrition_days (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  day           date NOT NULL,
  calories      integer NOT NULL DEFAULT 0 CHECK (calories >= 0),
  protein_grams integer NOT NULL DEFAULT 0 CHECK (protein_grams >= 0),
  carb_grams    integer NOT NULL DEFAULT 0 CHECK (carb_grams >= 0),
  fiber_grams   integer NOT NULL DEFAULT 0 CHECK (fiber_grams >= 0),
  water_ounces  integer NOT NULL DEFAULT 0 CHECK (water_ounces >= 0),
  deleted_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_nutrition_days_updated_at ON public.nutrition_days;
CREATE TRIGGER trg_nutrition_days_updated_at
  BEFORE UPDATE ON public.nutrition_days
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_nutrition_days_user_day
  ON public.nutrition_days(user_id, day) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_nutrition_days_user_day
  ON public.nutrition_days(user_id, day DESC) WHERE deleted_at IS NULL;

ALTER TABLE public.nutrition_days ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS nutrition_days_select ON public.nutrition_days;
CREATE POLICY nutrition_days_select ON public.nutrition_days
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS nutrition_days_insert ON public.nutrition_days;
CREATE POLICY nutrition_days_insert ON public.nutrition_days
  FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS nutrition_days_update ON public.nutrition_days;
CREATE POLICY nutrition_days_update ON public.nutrition_days
  FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Signup auto-provisioning
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO public.profiles (id, name)
  VALUES (NEW.id, COALESCE(NULLIF(NEW.raw_user_meta_data->>'name', ''), 'there'));

  INSERT INTO public.nutrition_goals (user_id)
  VALUES (NEW.id);

  INSERT INTO public.health_goals (user_id)
  VALUES (NEW.id);

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ---------------------------------------------------------------------------
-- food_entries (new): one row per accepted scan
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.food_entries (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  day            date NOT NULL,
  scan_type      text NOT NULL CHECK (scan_type IN ('food', 'beverage', 'water')),
  items          jsonb NOT NULL DEFAULT '[]'::jsonb,
  calories       integer NOT NULL DEFAULT 0 CHECK (calories >= 0),
  protein_grams  integer NOT NULL DEFAULT 0 CHECK (protein_grams >= 0),
  carb_grams     integer NOT NULL DEFAULT 0 CHECK (carb_grams >= 0),
  fiber_grams    integer NOT NULL DEFAULT 0 CHECK (fiber_grams >= 0),
  water_ounces   integer NOT NULL DEFAULT 0 CHECK (water_ounces >= 0),
  source         text NOT NULL DEFAULT 'snap',
  model          text,
  prompt_version text,
  deleted_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_food_entries_user_day
  ON public.food_entries(user_id, day DESC) WHERE deleted_at IS NULL;

ALTER TABLE public.food_entries ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS food_entries_select ON public.food_entries;
CREATE POLICY food_entries_select ON public.food_entries
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- No INSERT/UPDATE policies for authenticated: writes go through log_scan()
-- with the service role (server-authoritative).

-- ---------------------------------------------------------------------------
-- log_scan (new): atomic per-scan insert + daily increment
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_scan(
  p_user_id        uuid,
  p_scan_type      text,
  p_items          jsonb,
  p_calories       integer,
  p_protein_grams  integer,
  p_carb_grams     integer,
  p_fiber_grams    integer,
  p_water_ounces   integer,
  p_model          text,
  p_prompt_version text
)
RETURNS TABLE (
  day date, calories integer, protein_grams integer,
  carb_grams integer, fiber_grams integer, water_ounces integer
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_tz  text;
  v_day date;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  INSERT INTO public.food_entries
    (user_id, day, scan_type, items, calories, protein_grams,
     carb_grams, fiber_grams, water_ounces, model, prompt_version)
  VALUES
    (p_user_id, v_day, p_scan_type, COALESCE(p_items, '[]'::jsonb),
     GREATEST(p_calories, 0), GREATEST(p_protein_grams, 0),
     GREATEST(p_carb_grams, 0), GREATEST(p_fiber_grams, 0),
     GREATEST(p_water_ounces, 0), p_model, p_prompt_version);

  INSERT INTO public.nutrition_days AS nd
    (user_id, day, calories, protein_grams, carb_grams, fiber_grams, water_ounces)
  VALUES
    (p_user_id, v_day, GREATEST(p_calories, 0), GREATEST(p_protein_grams, 0),
     GREATEST(p_carb_grams, 0), GREATEST(p_fiber_grams, 0), GREATEST(p_water_ounces, 0))
  ON CONFLICT (user_id, day) WHERE deleted_at IS NULL
  DO UPDATE SET
    calories      = nd.calories      + EXCLUDED.calories,
    protein_grams = nd.protein_grams + EXCLUDED.protein_grams,
    carb_grams    = nd.carb_grams    + EXCLUDED.carb_grams,
    fiber_grams   = nd.fiber_grams   + EXCLUDED.fiber_grams,
    water_ounces  = nd.water_ounces  + EXCLUDED.water_ounces;

  RETURN QUERY
    SELECT nd.day, nd.calories, nd.protein_grams, nd.carb_grams,
           nd.fiber_grams, nd.water_ounces
    FROM public.nutrition_days nd
    WHERE nd.user_id = p_user_id AND nd.day = v_day AND nd.deleted_at IS NULL;
END;
$$;

-- Server-authoritative: only the service role may call it.
REVOKE ALL ON FUNCTION public.log_scan(uuid, text, jsonb, integer, integer,
  integer, integer, integer, text, text) FROM PUBLIC, anon, authenticated;
