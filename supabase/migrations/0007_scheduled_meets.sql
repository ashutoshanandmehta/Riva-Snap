-- "Schedule a call": server-orchestrated Cal.com bookings with a clinician or
-- wellness coach. Purely additive — no ALTER on any existing table.
--
-- Conventions follow 0005: soft delete, SELECT-only RLS, set_updated_at
-- trigger, and server-authoritative SECURITY DEFINER functions REVOKEd from
-- the client roles. The backend verifies the caller's bearer token, resolves
-- the booking with Cal.com itself, and only then calls create_scheduled_meet
-- with the verified user id — no client ever writes here directly.

-- ---------------------------------------------------------------------------
-- scheduled_meets: one booked (or cancelled) call
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.scheduled_meets (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  person_role      text NOT NULL CHECK (person_role IN ('clinician', 'wellness_coach')),
  person_name      text NOT NULL CHECK (length(trim(person_name)) > 0),
  reason_category  text NOT NULL CHECK (
                       reason_category IN (
                         'side_effects', 'dose_adjustment', 'general_checkin', 'other'
                       )
                     ),
  reason_note      text CHECK (reason_note IS NULL OR length(reason_note) <= 500),
  starts_at        timestamptz NOT NULL,
  ends_at          timestamptz NOT NULL CHECK (ends_at > starts_at),
  -- Cal.com's own booking identifier. Mock-mode bookings get a "mock-"
  -- prefixed synthetic uid so they still satisfy uniqueness.
  cal_booking_uid  text NOT NULL,
  meeting_url      text,
  status           text NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled')),
  is_mock          boolean NOT NULL DEFAULT false,
  deleted_at       timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT scheduled_meets_cal_booking_uid_unique UNIQUE (cal_booking_uid)
);

DROP TRIGGER IF EXISTS trg_scheduled_meets_updated_at ON public.scheduled_meets;
CREATE TRIGGER trg_scheduled_meets_updated_at
  BEFORE UPDATE ON public.scheduled_meets
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS idx_scheduled_meets_user_start
  ON public.scheduled_meets(user_id, starts_at);

ALTER TABLE public.scheduled_meets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS scheduled_meets_select ON public.scheduled_meets;
CREATE POLICY scheduled_meets_select ON public.scheduled_meets
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- create_scheduled_meet: insert one booking after Cal.com has confirmed it
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.create_scheduled_meet(
  p_user_id         uuid,
  p_person_role     text,
  p_person_name     text,
  p_reason_category text,
  p_reason_note     text,
  p_starts_at       timestamptz,
  p_ends_at         timestamptz,
  p_cal_booking_uid text,
  p_meeting_url     text,
  p_is_mock         boolean
)
RETURNS public.scheduled_meets
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_row public.scheduled_meets;
BEGIN
  INSERT INTO public.scheduled_meets (
    user_id, person_role, person_name, reason_category, reason_note,
    starts_at, ends_at, cal_booking_uid, meeting_url, is_mock
  )
  VALUES (
    p_user_id, p_person_role, p_person_name, p_reason_category, p_reason_note,
    p_starts_at, p_ends_at, p_cal_booking_uid, p_meeting_url, COALESCE(p_is_mock, false)
  )
  RETURNING * INTO v_row;

  RETURN v_row;
END;
$$;

REVOKE ALL ON FUNCTION public.create_scheduled_meet(
  uuid, text, text, text, text, timestamptz, timestamptz, text, text, boolean
) FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- list_scheduled_meets: the user's upcoming, confirmed, non-deleted calls
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.list_scheduled_meets(
  p_user_id uuid,
  p_limit   integer DEFAULT 20
)
RETURNS SETOF public.scheduled_meets
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    SELECT m.*
    FROM public.scheduled_meets m
    WHERE m.user_id = p_user_id
      AND m.starts_at >= now()
      AND m.status = 'confirmed'
      AND m.deleted_at IS NULL
    ORDER BY m.starts_at ASC
    LIMIT GREATEST(COALESCE(p_limit, 20), 1);
END;
$$;

REVOKE ALL ON FUNCTION public.list_scheduled_meets(uuid, integer)
  FROM PUBLIC, anon, authenticated;
