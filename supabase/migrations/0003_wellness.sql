-- Riva wellness: session logging, streaks, daily minutes goal, and the
-- day-cached AI suggestion payloads. Conventions follow 0002: soft delete,
-- SELECT-only RLS, set_updated_at trigger, and server-authoritative
-- functions (service role only). All day/streak math runs in SQL against the
-- profile timezone, matching log_scan/log_checkin.

-- ---------------------------------------------------------------------------
-- nutrition_goals: user-editable daily wellness minutes goal
-- ---------------------------------------------------------------------------

ALTER TABLE public.nutrition_goals
  ADD COLUMN IF NOT EXISTS wellness_minutes_goal integer NOT NULL DEFAULT 45
    CHECK (wellness_minutes_goal >= 0);

-- ---------------------------------------------------------------------------
-- wellness_sessions: one row per completed practice
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.wellness_sessions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  day         date NOT NULL,
  practice_id text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('yoga', 'meditation', 'exercise', 'mind', 'sleep')),
  minutes     integer NOT NULL CHECK (minutes > 0 AND minutes <= 300),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz
);

DROP TRIGGER IF EXISTS trg_wellness_sessions_updated_at ON public.wellness_sessions;
CREATE TRIGGER trg_wellness_sessions_updated_at
  BEFORE UPDATE ON public.wellness_sessions
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_wellness_sessions_user_day
  ON public.wellness_sessions(user_id, day DESC) WHERE deleted_at IS NULL;

ALTER TABLE public.wellness_sessions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS wellness_sessions_select ON public.wellness_sessions;
CREATE POLICY wellness_sessions_select ON public.wellness_sessions
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- wellness_suggestions: per-user, per-day LLM suggestion cache
-- (server-only — no client policies; the service role bypasses RLS)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.wellness_suggestions (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  day        date NOT NULL,
  payload    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, day)
);

ALTER TABLE public.wellness_suggestions ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- wellness_streak: consecutive distinct practice days ending at the anchor
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.wellness_streak(
  p_user_id uuid,
  p_anchor  date
)
RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  -- Distinct days descending; a day belongs to the streak while it stays in
  -- lockstep with its row number (anchor, anchor-1, ...). The first gap makes
  -- day fall behind rn permanently, so the count is exactly the streak.
  SELECT count(*)::integer
  FROM (
    SELECT d.day, row_number() OVER (ORDER BY d.day DESC) AS rn
    FROM (
      SELECT DISTINCT ws.day
      FROM public.wellness_sessions ws
      WHERE ws.user_id = p_user_id AND ws.deleted_at IS NULL AND ws.day <= p_anchor
    ) d
  ) t
  WHERE t.day = p_anchor - (t.rn - 1)::integer;
$$;

REVOKE ALL ON FUNCTION public.wellness_streak(uuid, date)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- log_wellness_session: insert one session, return the day's running summary
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_wellness_session(
  p_user_id     uuid,
  p_practice_id text,
  p_kind        text,
  p_minutes     integer
)
RETURNS TABLE (day date, minutes_today integer, streak_days integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz  text;
  v_day date;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  INSERT INTO public.wellness_sessions (user_id, day, practice_id, kind, minutes)
  VALUES (p_user_id, v_day, p_practice_id, p_kind, p_minutes);

  RETURN QUERY
    SELECT v_day,
           COALESCE(sum(ws.minutes), 0)::integer,
           public.wellness_streak(p_user_id, v_day)
    FROM public.wellness_sessions ws
    WHERE ws.user_id = p_user_id AND ws.day = v_day AND ws.deleted_at IS NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.log_wellness_session(uuid, text, text, integer)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- wellness_summary: read-side summary for the dashboard
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.wellness_summary(
  p_user_id uuid
)
RETURNS TABLE (day date, minutes_today integer, streak_days integer)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz      text;
  v_day     date;
  v_minutes integer;
  v_streak  integer;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  SELECT COALESCE(sum(ws.minutes), 0)::integer INTO v_minutes
  FROM public.wellness_sessions ws
  WHERE ws.user_id = p_user_id AND ws.day = v_day AND ws.deleted_at IS NULL;

  v_streak := public.wellness_streak(p_user_id, v_day);
  IF v_streak = 0 THEN
    -- Nothing logged yet today: an unbroken streak survives until end of day.
    v_streak := public.wellness_streak(p_user_id, v_day - 1);
  END IF;

  RETURN QUERY SELECT v_day, v_minutes, v_streak;
END;
$$;

REVOKE ALL ON FUNCTION public.wellness_summary(uuid)
  FROM PUBLIC, anon, authenticated;
