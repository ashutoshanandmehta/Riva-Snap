-- Adds "maintain weight" as a health goal alongside "lose weight".
--
-- The onboarding intake form now offers exactly these two, so `weight_mgmt`
-- (lose) needed a counterpart. Additive and idempotent, matching 0003's
-- ADD COLUMN IF NOT EXISTS style; existing rows default to false, which is
-- correct — nobody has chosen it yet.
--
-- Apply this BEFORE deploying the backend that selects the column:
-- `_HEALTH_GOAL_COLUMNS` names it explicitly, so PostgREST 400s on a database
-- that does not have it yet.

ALTER TABLE public.health_goals
  ADD COLUMN IF NOT EXISTS weight_maintain boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.health_goals.weight_maintain IS
  'User wants to hold their current weight rather than lose. Mutually '
  'exclusive with weight_mgmt in the intake form, but stored independently '
  'so an existing account can carry both while goals are being revised.';
