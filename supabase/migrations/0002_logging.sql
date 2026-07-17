-- Riva logging: medication plans, shots, weights, side effects, check-ins.
-- Tables are verbatim from FINAL_DATABASE_SCHEMA.md with IF NOT EXISTS
-- guards. The log_* functions are the server-authoritative write path
-- (service role only), matching log_scan from 0001.

-- ---------------------------------------------------------------------------
-- medication_plans (needed by shots; onboarding fills the rest later)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.medication_plans (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name                 text NOT NULL,
  current_dose_mg      numeric(6,2) NOT NULL CHECK (current_dose_mg >= 0),
  cadence_days         integer NOT NULL DEFAULT 7 CHECK (cadence_days BETWEEN 1 AND 90),
  dose_frequency       text NOT NULL DEFAULT 'weekly' CHECK (dose_frequency IN ('weekly', 'daily', 'other')),
  start_date           date,
  reminder_description text,
  is_active            boolean NOT NULL DEFAULT true,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_medication_plans_updated_at ON public.medication_plans;
CREATE TRIGGER trg_medication_plans_updated_at
  BEFORE UPDATE ON public.medication_plans
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_medication_plans_active
  ON public.medication_plans(user_id) WHERE is_active = true;

CREATE INDEX IF NOT EXISTS idx_medication_plans_user ON public.medication_plans(user_id);

ALTER TABLE public.medication_plans ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS medication_plans_select ON public.medication_plans;
CREATE POLICY medication_plans_select ON public.medication_plans
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- shots
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.shots (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plan_id         uuid REFERENCES public.medication_plans(id) ON DELETE SET NULL,
  medication_name text NOT NULL,
  dose_mg         numeric(6,2) NOT NULL CHECK (dose_mg > 0),
  taken_at        timestamptz NOT NULL DEFAULT now(),
  injection_site  text NOT NULL CHECK (injection_site IN (
                    'right_arm', 'left_arm', 'lower_left_abs',
                    'lower_right_abs', 'right_thigh', 'left_thigh'
                  )),
  comfort_rating  smallint CHECK (comfort_rating BETWEEN 1 AND 5),
  deleted_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_shots_updated_at ON public.shots;
CREATE TRIGGER trg_shots_updated_at
  BEFORE UPDATE ON public.shots
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_shots_user_taken
  ON public.shots(user_id, taken_at DESC) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_shots_plan ON public.shots(plan_id);

ALTER TABLE public.shots ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS shots_select ON public.shots;
CREATE POLICY shots_select ON public.shots
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- weights
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.weights (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  pounds      numeric(6,2) NOT NULL CHECK (pounds > 0),
  dose_mg     numeric(6,2),
  measured_at timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_weights_updated_at ON public.weights;
CREATE TRIGGER trg_weights_updated_at
  BEFORE UPDATE ON public.weights
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_weights_user_measured
  ON public.weights(user_id, measured_at DESC) WHERE deleted_at IS NULL;

ALTER TABLE public.weights ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS weights_select ON public.weights;
CREATE POLICY weights_select ON public.weights
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- side_effect_logs and side_effect_log_items
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.side_effect_logs (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  log_date   date NOT NULL,
  note       text,
  deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_side_effect_logs_updated_at ON public.side_effect_logs;
CREATE TRIGGER trg_side_effect_logs_updated_at
  BEFORE UPDATE ON public.side_effect_logs
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_side_effect_logs_user_date
  ON public.side_effect_logs(user_id, log_date) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_side_effect_logs_user_date
  ON public.side_effect_logs(user_id, log_date DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS public.side_effect_log_items (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  log_id   uuid NOT NULL REFERENCES public.side_effect_logs(id) ON DELETE CASCADE,
  user_id  uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  effect   text NOT NULL CHECK (effect IN (
             'nausea', 'headache', 'fatigue', 'constipation', 'diarrhea',
             'dizziness', 'bloating', 'heartburn', 'food_noise'
           )),
  severity smallint NOT NULL CHECK (severity BETWEEN 1 AND 5),
  UNIQUE (log_id, effect)
);

CREATE INDEX IF NOT EXISTS idx_se_items_log ON public.side_effect_log_items(log_id);
CREATE INDEX IF NOT EXISTS idx_se_items_user_effect ON public.side_effect_log_items(user_id, effect);

ALTER TABLE public.side_effect_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.side_effect_log_items ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS side_effect_logs_select ON public.side_effect_logs;
CREATE POLICY side_effect_logs_select ON public.side_effect_logs
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS side_effect_log_items_select ON public.side_effect_log_items;
CREATE POLICY side_effect_log_items_select ON public.side_effect_log_items
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- checkin_questions, checkin_options (global config), checkins, answers
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.checkin_questions (
  id         text PRIMARY KEY,
  category   text NOT NULL,
  title      text NOT NULL,
  subtitle   text,
  symbol     text NOT NULL,
  sort_order integer NOT NULL DEFAULT 0,
  is_active  boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS public.checkin_options (
  question_id text NOT NULL REFERENCES public.checkin_questions(id) ON DELETE CASCADE,
  code        text NOT NULL,
  label       text NOT NULL,
  symbol      text NOT NULL,
  value       smallint NOT NULL CHECK (value BETWEEN 1 AND 5),
  position    smallint NOT NULL,
  PRIMARY KEY (question_id, code)
);

CREATE TABLE IF NOT EXISTS public.checkins (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  checkin_date date NOT NULL,
  deleted_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS trg_checkins_updated_at ON public.checkins;
CREATE TRIGGER trg_checkins_updated_at
  BEFORE UPDATE ON public.checkins
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE UNIQUE INDEX IF NOT EXISTS uq_checkins_user_date
  ON public.checkins(user_id, checkin_date) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_checkins_user_date
  ON public.checkins(user_id, checkin_date DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS public.checkin_answers (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  checkin_id  uuid NOT NULL REFERENCES public.checkins(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  question_id text NOT NULL,
  option_code text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (checkin_id, question_id),
  FOREIGN KEY (question_id, option_code)
    REFERENCES public.checkin_options(question_id, code)
);

CREATE INDEX IF NOT EXISTS idx_checkin_answers_checkin ON public.checkin_answers(checkin_id);
CREATE INDEX IF NOT EXISTS idx_checkin_answers_user_question ON public.checkin_answers(user_id, question_id);

ALTER TABLE public.checkin_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkin_options ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkins ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.checkin_answers ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS checkin_questions_select ON public.checkin_questions;
CREATE POLICY checkin_questions_select ON public.checkin_questions
  FOR SELECT TO authenticated
  USING (true);

DROP POLICY IF EXISTS checkin_options_select ON public.checkin_options;
CREATE POLICY checkin_options_select ON public.checkin_options
  FOR SELECT TO authenticated
  USING (true);

DROP POLICY IF EXISTS checkins_select ON public.checkins;
CREATE POLICY checkins_select ON public.checkins
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

DROP POLICY IF EXISTS checkin_answers_select ON public.checkin_answers;
CREATE POLICY checkin_answers_select ON public.checkin_answers
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- Seed check-in questions and options (doc: mood, energy, sleep, nausea,
-- appetite). value 5 is always the best state.

INSERT INTO public.checkin_questions (id, category, title, subtitle, symbol, sort_order) VALUES
  ('mood',     'wellbeing', 'Mood',          'How are you feeling today?',    'face.smiling', 1),
  ('energy',   'wellbeing', 'Energy',        'How is your energy level?',     'bolt',         2),
  ('sleep',    'wellbeing', 'Sleep Quality', 'How did you sleep last night?', 'moon.zzz',     3),
  ('nausea',   'symptoms',  'Nausea',        'Any nausea today?',             'stomach',      4),
  ('appetite', 'symptoms',  'Appetite',      'How is your appetite?',         'fork.knife',   5)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.checkin_options (question_id, code, label, symbol, value, position) VALUES
  ('mood', 'awful', 'Awful', 'cloud.rain',      1, 1),
  ('mood', 'low',   'Low',   'cloud',           2, 2),
  ('mood', 'okay',  'Okay',  'cloud.sun',       3, 3),
  ('mood', 'good',  'Good',  'sun.min',         4, 4),
  ('mood', 'great', 'Great', 'sun.max',         5, 5),
  ('energy', 'drained', 'Drained', 'battery.0percent',   1, 1),
  ('energy', 'low',     'Low',     'battery.25percent',  2, 2),
  ('energy', 'okay',    'Okay',    'battery.50percent',  3, 3),
  ('energy', 'good',    'Good',    'battery.75percent',  4, 4),
  ('energy', 'high',    'High',    'battery.100percent', 5, 5),
  ('sleep', 'terrible',  'Terrible',  'moon.zzz', 1, 1),
  ('sleep', 'poor',      'Poor',      'moon.zzz', 2, 2),
  ('sleep', 'okay',      'Okay',      'moon.zzz', 3, 3),
  ('sleep', 'good',      'Good',      'moon.zzz', 4, 4),
  ('sleep', 'excellent', 'Excellent', 'moon.zzz', 5, 5),
  ('nausea', 'severe',   'Severe',   'exclamationmark.3', 1, 1),
  ('nausea', 'strong',   'Strong',   'exclamationmark.2', 2, 2),
  ('nausea', 'moderate', 'Moderate', 'exclamationmark',   3, 3),
  ('nausea', 'mild',     'Mild',     'minus',             4, 4),
  ('nausea', 'none',     'None',     'checkmark',         5, 5),
  ('appetite', 'none',   'None',   'fork.knife', 1, 1),
  ('appetite', 'low',    'Low',    'fork.knife', 2, 2),
  ('appetite', 'okay',   'Okay',   'fork.knife', 3, 3),
  ('appetite', 'good',   'Good',   'fork.knife', 4, 4),
  ('appetite', 'strong', 'Strong', 'fork.knife', 5, 5)
ON CONFLICT (question_id, code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- log_shot: shot insert + medication plan sync in one transaction
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_shot(
  p_user_id         uuid,
  p_medication_name text,
  p_dose_mg         numeric,
  p_injection_site  text,
  p_comfort_rating  integer,
  p_taken_at        timestamptz
)
RETURNS TABLE (
  shot_id uuid, medication_name text, dose_mg numeric,
  taken_at timestamptz, injection_site text
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_plan_id uuid;
  v_shot_id uuid;
BEGIN
  SELECT mp.id INTO v_plan_id
  FROM public.medication_plans mp
  WHERE mp.user_id = p_user_id AND mp.is_active = true
  LIMIT 1;

  IF v_plan_id IS NULL THEN
    INSERT INTO public.medication_plans (user_id, name, current_dose_mg, start_date)
    VALUES (p_user_id, p_medication_name, p_dose_mg, CURRENT_DATE)
    RETURNING id INTO v_plan_id;
  ELSE
    -- Doc rule: logging a shot must sync the plan's current dose.
    UPDATE public.medication_plans mp
    SET current_dose_mg = p_dose_mg
    WHERE mp.id = v_plan_id;
  END IF;

  INSERT INTO public.shots
    (user_id, plan_id, medication_name, dose_mg, taken_at, injection_site, comfort_rating)
  VALUES
    (p_user_id, v_plan_id, p_medication_name, p_dose_mg,
     COALESCE(p_taken_at, now()), p_injection_site, p_comfort_rating)
  RETURNING id INTO v_shot_id;

  RETURN QUERY
    SELECT s.id, s.medication_name, s.dose_mg, s.taken_at, s.injection_site
    FROM public.shots s WHERE s.id = v_shot_id;
END;
$$;

REVOKE ALL ON FUNCTION public.log_shot(uuid, text, numeric, text, integer, timestamptz)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- log_weight: weight insert with dose snapshot from the active plan
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_weight(
  p_user_id     uuid,
  p_pounds      numeric,
  p_measured_at timestamptz
)
RETURNS TABLE (weight_id uuid, pounds numeric, dose_mg numeric, measured_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_dose      numeric;
  v_weight_id uuid;
BEGIN
  SELECT mp.current_dose_mg INTO v_dose
  FROM public.medication_plans mp
  WHERE mp.user_id = p_user_id AND mp.is_active = true
  LIMIT 1;

  INSERT INTO public.weights (user_id, pounds, dose_mg, measured_at)
  VALUES (p_user_id, p_pounds, v_dose, COALESCE(p_measured_at, now()))
  RETURNING id INTO v_weight_id;

  RETURN QUERY
    SELECT w.id, w.pounds, w.dose_mg, w.measured_at
    FROM public.weights w WHERE w.id = v_weight_id;
END;
$$;

REVOKE ALL ON FUNCTION public.log_weight(uuid, numeric, timestamptz)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- log_side_effects: replace today's set of effects in one transaction
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_side_effects(
  p_user_id uuid,
  p_effects jsonb,
  p_note    text
)
RETURNS TABLE (log_date date, effect text, severity smallint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz     text;
  v_day    date;
  v_log_id uuid;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  INSERT INTO public.side_effect_logs AS sel (user_id, log_date, note)
  VALUES (p_user_id, v_day, p_note)
  ON CONFLICT (user_id, log_date) WHERE deleted_at IS NULL
  DO UPDATE SET note = COALESCE(EXCLUDED.note, sel.note)
  RETURNING id INTO v_log_id;

  -- The sheet submits the full current selection; replace the day's set.
  DELETE FROM public.side_effect_log_items WHERE log_id = v_log_id;

  INSERT INTO public.side_effect_log_items (log_id, user_id, effect, severity)
  SELECT v_log_id, p_user_id, item->>'effect', (item->>'severity')::smallint
  FROM jsonb_array_elements(COALESCE(p_effects, '[]'::jsonb)) AS item;

  RETURN QUERY
    SELECT v_day, i.effect, i.severity
    FROM public.side_effect_log_items i
    WHERE i.log_id = v_log_id
    ORDER BY i.effect;
END;
$$;

REVOKE ALL ON FUNCTION public.log_side_effects(uuid, jsonb, text)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- log_checkin: answer one check-in question for today (e.g. sleep quality)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_checkin(
  p_user_id     uuid,
  p_question_id text,
  p_option_code text
)
RETURNS TABLE (checkin_date date, question_id text, option_code text, label text, value smallint)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz         text;
  v_day        date;
  v_checkin_id uuid;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  INSERT INTO public.checkins AS c (user_id, checkin_date)
  VALUES (p_user_id, v_day)
  ON CONFLICT (user_id, checkin_date) WHERE deleted_at IS NULL
  DO UPDATE SET updated_at = now()
  RETURNING id INTO v_checkin_id;

  INSERT INTO public.checkin_answers (checkin_id, user_id, question_id, option_code)
  VALUES (v_checkin_id, p_user_id, p_question_id, p_option_code)
  ON CONFLICT (checkin_id, question_id)
  DO UPDATE SET option_code = EXCLUDED.option_code;

  RETURN QUERY
    SELECT v_day, o.question_id, o.code, o.label, o.value
    FROM public.checkin_options o
    WHERE o.question_id = p_question_id AND o.code = p_option_code;
END;
$$;

REVOKE ALL ON FUNCTION public.log_checkin(uuid, text, text)
  FROM PUBLIC, anon, authenticated;
