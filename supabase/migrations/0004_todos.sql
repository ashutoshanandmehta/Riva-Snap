-- Riva to-dos: user-set reminders shown on the Home card, grouped by category
-- (food / water / weight / custom) and fired locally by the app at their time.
-- Conventions follow 0002/0003: soft delete, SELECT-only RLS, set_updated_at
-- trigger, and server-authoritative SECURITY DEFINER functions (service role
-- only). Day math runs in SQL against the profile timezone, matching
-- log_side_effects/log_wellness_session.

-- ---------------------------------------------------------------------------
-- todos: one row per to-do the user set
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.todos (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title         text NOT NULL CHECK (char_length(btrim(title)) BETWEEN 1 AND 80),
  category      text NOT NULL CHECK (category IN ('food', 'water', 'weight', 'custom')),
  repeat_rule   text NOT NULL CHECK (repeat_rule IN ('daily', 'once')),
  remind_hour   integer NOT NULL CHECK (remind_hour BETWEEN 0 AND 23),
  remind_minute integer NOT NULL CHECK (remind_minute BETWEEN 0 AND 59),
  -- The calendar day a 'once' to-do fires; always NULL for 'daily'.
  due_date      date,
  -- The last profile-timezone day the user ticked it. A 'daily' to-do is done
  -- when this equals today, so it resets itself each morning with no cron and
  -- no client-side day math. A 'once' to-do is done once this is set at all.
  completed_on  date,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  deleted_at    timestamptz,
  CONSTRAINT todos_due_date_matches_repeat
    CHECK ((repeat_rule = 'once') = (due_date IS NOT NULL))
);

DROP TRIGGER IF EXISTS trg_todos_updated_at ON public.todos;
CREATE TRIGGER trg_todos_updated_at
  BEFORE UPDATE ON public.todos
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_todos_user_created
  ON public.todos(user_id, created_at) WHERE deleted_at IS NULL;

ALTER TABLE public.todos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS todos_select ON public.todos;
CREATE POLICY todos_select ON public.todos
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- list_todos: open to-dos with done state resolved against the profile day
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.list_todos(
  p_user_id uuid
)
RETURNS TABLE (
  id uuid,
  title text,
  category text,
  repeat_rule text,
  remind_hour integer,
  remind_minute integer,
  due_date date,
  is_done boolean
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz  text;
  v_day date;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  RETURN QUERY
    SELECT t.id,
           t.title,
           t.category,
           t.repeat_rule,
           t.remind_hour,
           t.remind_minute,
           t.due_date,
           CASE WHEN t.repeat_rule = 'daily' THEN COALESCE(t.completed_on = v_day, false)
                ELSE t.completed_on IS NOT NULL
           END
    FROM public.todos t
    WHERE t.user_id = p_user_id
      AND t.deleted_at IS NULL
      -- A one-off ticked on an earlier day is finished business; it drops off
      -- the card rather than lingering as a permanent checkmark.
      AND NOT (t.repeat_rule = 'once' AND t.completed_on IS NOT NULL AND t.completed_on < v_day)
    ORDER BY t.remind_hour, t.remind_minute, t.created_at;
END;
$$;

REVOKE ALL ON FUNCTION public.list_todos(uuid)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- upsert_todo: insert when p_todo_id is NULL, else update that owned row.
-- Backs both "Set a to-do" and "Edit to-do".
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.upsert_todo(
  p_user_id       uuid,
  p_todo_id       uuid,
  p_title         text,
  p_category      text,
  p_repeat_rule   text,
  p_remind_hour   integer,
  p_remind_minute integer,
  p_due_date      date
)
RETURNS TABLE (
  id uuid,
  title text,
  category text,
  repeat_rule text,
  remind_hour integer,
  remind_minute integer,
  due_date date,
  is_done boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz      text;
  v_day     date;
  v_todo_id uuid;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  IF p_todo_id IS NULL THEN
    INSERT INTO public.todos (
      user_id, title, category, repeat_rule, remind_hour, remind_minute, due_date
    )
    VALUES (
      p_user_id, btrim(p_title), p_category, p_repeat_rule,
      p_remind_hour, p_remind_minute, p_due_date
    )
    RETURNING todos.id INTO v_todo_id;
  ELSE
    -- Scoped to the caller, so a forged id cannot touch another account.
    UPDATE public.todos t
       SET title         = btrim(p_title),
           category      = p_category,
           repeat_rule   = p_repeat_rule,
           remind_hour   = p_remind_hour,
           remind_minute = p_remind_minute,
           due_date      = p_due_date,
           -- completed_on means different things under each rule ("done today"
           -- vs "done for good"), so carrying it across a reschedule
           -- reinterprets it. A daily to-do ticked yesterday and switched to
           -- once would read as finished business and drop off the card; a
           -- once already ticked and moved to a new date would vanish the same
           -- way. Rescheduling means pending again, which is what an edit
           -- implies. Renaming alone keeps the checkmark.
           completed_on  = CASE
                             WHEN t.repeat_rule IS DISTINCT FROM p_repeat_rule THEN NULL
                             WHEN t.due_date IS DISTINCT FROM p_due_date THEN NULL
                             ELSE t.completed_on
                           END
     WHERE t.id = p_todo_id AND t.user_id = p_user_id AND t.deleted_at IS NULL
    RETURNING t.id INTO v_todo_id;
  END IF;

  IF v_todo_id IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
    SELECT t.id,
           t.title,
           t.category,
           t.repeat_rule,
           t.remind_hour,
           t.remind_minute,
           t.due_date,
           CASE WHEN t.repeat_rule = 'daily' THEN COALESCE(t.completed_on = v_day, false)
                ELSE t.completed_on IS NOT NULL
           END
    FROM public.todos t
    -- v_todo_id already came from an ownership-filtered write; the predicate is
    -- repeated so this stays safe if that ever changes.
    WHERE t.id = v_todo_id AND t.user_id = p_user_id;
END;
$$;

REVOKE ALL ON FUNCTION public.upsert_todo(uuid, uuid, text, text, text, integer, integer, date)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- set_todo_done: tick or untick for the profile-timezone day
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.set_todo_done(
  p_user_id uuid,
  p_todo_id uuid,
  p_done    boolean
)
RETURNS TABLE (
  id uuid,
  title text,
  category text,
  repeat_rule text,
  remind_hour integer,
  remind_minute integer,
  due_date date,
  is_done boolean
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_tz  text;
  v_day date;
BEGIN
  SELECT p.timezone INTO v_tz FROM public.profiles p WHERE p.id = p_user_id;
  v_day := (now() AT TIME ZONE COALESCE(v_tz, 'America/New_York'))::date;

  UPDATE public.todos t
     SET completed_on = CASE WHEN p_done THEN v_day ELSE NULL END
   WHERE t.id = p_todo_id AND t.user_id = p_user_id AND t.deleted_at IS NULL;

  RETURN QUERY
    SELECT t.id,
           t.title,
           t.category,
           t.repeat_rule,
           t.remind_hour,
           t.remind_minute,
           t.due_date,
           CASE WHEN t.repeat_rule = 'daily' THEN COALESCE(t.completed_on = v_day, false)
                ELSE t.completed_on IS NOT NULL
           END
    FROM public.todos t
    WHERE t.id = p_todo_id AND t.user_id = p_user_id AND t.deleted_at IS NULL;
END;
$$;

REVOKE ALL ON FUNCTION public.set_todo_done(uuid, uuid, boolean)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- delete_todo: soft delete one owned to-do
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.delete_todo(
  p_user_id uuid,
  p_todo_id uuid
)
RETURNS TABLE (id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
BEGIN
  -- RETURN QUERY takes a SELECT, so the UPDATE runs inside a CTE.
  RETURN QUERY
    WITH removed AS (
      UPDATE public.todos t
         SET deleted_at = now()
       WHERE t.id = p_todo_id AND t.user_id = p_user_id AND t.deleted_at IS NULL
      RETURNING t.id
    )
    SELECT removed.id FROM removed;
END;
$$;

REVOKE ALL ON FUNCTION public.delete_todo(uuid, uuid)
  FROM PUBLIC, anon, authenticated;
