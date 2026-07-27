-- Riva AI companion chat: conversation threads and the messages in them.
-- Conventions follow 0002/0004: soft delete, SELECT-only RLS, set_updated_at
-- trigger, and server-authoritative SECURITY DEFINER functions REVOKEd from the
-- client roles. The backend verifies the caller's bearer token and passes the
-- verified user id; no client ever writes here directly.
--
-- These tables hold health-related free text (the user's own questions and the
-- companion's answers), so they are covered by the same isolation guarantees as
-- the rest of the schema: RLS scoped to auth.uid(), ownership re-checked inside
-- every function, and a cascade from auth.users so account deletion takes the
-- transcripts with it.

-- ---------------------------------------------------------------------------
-- chat_threads: one conversation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.chat_threads (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  -- Derived from the first user message by log_chat_message, so starting a
  -- thread costs no extra round trip. NULL until that first turn lands.
  title      text CHECK (title IS NULL OR char_length(btrim(title)) BETWEEN 1 AND 120),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz
);

DROP TRIGGER IF EXISTS trg_chat_threads_updated_at ON public.chat_threads;
CREATE TRIGGER trg_chat_threads_updated_at
  BEFORE UPDATE ON public.chat_threads
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- The history list is ordered by last activity.
CREATE INDEX IF NOT EXISTS idx_chat_threads_user_updated
  ON public.chat_threads(user_id, updated_at DESC) WHERE deleted_at IS NULL;

ALTER TABLE public.chat_threads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chat_threads_select ON public.chat_threads;
CREATE POLICY chat_threads_select ON public.chat_threads
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- chat_messages: one turn, user or assistant
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.chat_messages (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Insert order, and the ONLY thing turn order is read from. created_at cannot
  -- do this job: now() is transaction-scoped, so several messages written in one
  -- transaction share a timestamp, and a uuid tiebreak would scramble the
  -- transcript — which would replay a conversation to the model out of order.
  seq            bigserial NOT NULL,
  thread_id      uuid NOT NULL REFERENCES public.chat_threads(id) ON DELETE CASCADE,
  -- Denormalised from the thread so RLS and the privacy export can filter
  -- messages without a join, matching side_effect_log_items.
  user_id        uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  role           text NOT NULL CHECK (role IN ('user', 'assistant')),
  content        text NOT NULL DEFAULT '',
  -- One entry per tool the request actually executed: {tool, arguments, data}.
  -- Carried on assistant rows so the app can re-render the structured result
  -- behind an answer without re-running the tool. Always [] on user rows.
  tool_calls     jsonb NOT NULL DEFAULT '[]'::jsonb,
  model          text,
  prompt_version text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  deleted_at     timestamptz,
  CONSTRAINT chat_messages_tool_calls_is_array
    CHECK (jsonb_typeof(tool_calls) = 'array'),
  -- A user turn is the person's own words: it can never carry tool output or
  -- model metadata, so a bug that mislabels a role fails loudly here instead of
  -- quietly replaying tool results back as if the user had said them.
  CONSTRAINT chat_messages_user_rows_are_plain
    CHECK (
      role <> 'user'
      OR (tool_calls = '[]'::jsonb AND model IS NULL AND prompt_version IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_thread_seq
  ON public.chat_messages(thread_id, seq) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_chat_messages_user
  ON public.chat_messages(user_id) WHERE deleted_at IS NULL;

ALTER TABLE public.chat_messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chat_messages_select ON public.chat_messages;
CREATE POLICY chat_messages_select ON public.chat_messages
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- start_chat_thread: open a conversation
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.start_chat_thread(
  p_user_id uuid,
  p_title   text DEFAULT NULL
)
RETURNS TABLE (id uuid, title text, created_at timestamptz, updated_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_thread_id uuid;
BEGIN
  INSERT INTO public.chat_threads (user_id, title)
  VALUES (p_user_id, NULLIF(btrim(COALESCE(p_title, '')), ''))
  RETURNING chat_threads.id INTO v_thread_id;

  RETURN QUERY
    SELECT t.id, t.title, t.created_at, t.updated_at
    FROM public.chat_threads t
    WHERE t.id = v_thread_id;
END;
$$;

REVOKE ALL ON FUNCTION public.start_chat_thread(uuid, text)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- log_chat_message: append one turn to an owned thread
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.log_chat_message(
  p_user_id        uuid,
  p_thread_id      uuid,
  p_role           text,
  p_content        text,
  p_tool_calls     jsonb DEFAULT '[]'::jsonb,
  p_model          text DEFAULT NULL,
  p_prompt_version text DEFAULT NULL
)
RETURNS TABLE (
  id uuid,
  thread_id uuid,
  role text,
  content text,
  tool_calls jsonb,
  model text,
  prompt_version text,
  created_at timestamptz
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_message_id uuid;
BEGIN
  -- Ownership is the whole barrier here: a forged or someone else's thread id
  -- must write nothing at all, and the route turns the empty result into a 404.
  PERFORM 1
    FROM public.chat_threads t
   WHERE t.id = p_thread_id
     AND t.user_id = p_user_id
     AND t.deleted_at IS NULL;
  IF NOT FOUND THEN
    RETURN;
  END IF;

  INSERT INTO public.chat_messages (
    thread_id, user_id, role, content, tool_calls, model, prompt_version
  )
  VALUES (
    p_thread_id,
    p_user_id,
    p_role,
    COALESCE(p_content, ''),
    COALESCE(p_tool_calls, '[]'::jsonb),
    p_model,
    p_prompt_version
  )
  RETURNING chat_messages.id INTO v_message_id;

  -- Touch the thread so the history list re-sorts (updated_at comes from the
  -- set_updated_at trigger), and let the first user turn name it.
  UPDATE public.chat_threads t
     SET title = COALESCE(
           t.title,
           CASE
             WHEN p_role = 'user'
               THEN NULLIF(btrim(left(COALESCE(p_content, ''), 120)), '')
           END
         )
   WHERE t.id = p_thread_id;

  RETURN QUERY
    SELECT m.id,
           m.thread_id,
           m.role,
           m.content,
           m.tool_calls,
           m.model,
           m.prompt_version,
           m.created_at
    FROM public.chat_messages m
    WHERE m.id = v_message_id;
END;
$$;

REVOKE ALL ON FUNCTION public.log_chat_message(uuid, uuid, text, text, jsonb, text, text)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- list_chat_messages: one owned thread's turns, oldest-first
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.list_chat_messages(
  p_user_id   uuid,
  p_thread_id uuid,
  p_limit     integer DEFAULT 50
)
RETURNS TABLE (
  id uuid,
  role text,
  content text,
  tool_calls jsonb,
  model text,
  prompt_version text,
  created_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    -- The newest p_limit turns, handed back oldest-first: a prompt replays the
    -- conversation in order, but a long thread has to drop its oldest turns
    -- rather than its most recent ones.
    SELECT recent.id,
           recent.role,
           recent.content,
           recent.tool_calls,
           recent.model,
           recent.prompt_version,
           recent.created_at
    FROM (
      SELECT m.id,
             m.seq,
             m.role,
             m.content,
             m.tool_calls,
             m.model,
             m.prompt_version,
             m.created_at
      FROM public.chat_messages m
      JOIN public.chat_threads t ON t.id = m.thread_id
      WHERE m.thread_id = p_thread_id
        AND t.user_id = p_user_id
        AND m.deleted_at IS NULL
        AND t.deleted_at IS NULL
      ORDER BY m.seq DESC
      LIMIT GREATEST(COALESCE(p_limit, 50), 1)
    ) AS recent
    ORDER BY recent.seq;
END;
$$;

REVOKE ALL ON FUNCTION public.list_chat_messages(uuid, uuid, integer)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- list_chat_threads: the user's conversations, most recently active first
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.list_chat_threads(
  p_user_id uuid,
  p_limit   integer DEFAULT 50
)
RETURNS TABLE (
  id uuid,
  title text,
  message_count bigint,
  created_at timestamptz,
  updated_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
BEGIN
  RETURN QUERY
    SELECT t.id,
           t.title,
           count(m.id),
           t.created_at,
           t.updated_at
    FROM public.chat_threads t
    LEFT JOIN public.chat_messages m
           ON m.thread_id = t.id AND m.deleted_at IS NULL
    WHERE t.user_id = p_user_id
      AND t.deleted_at IS NULL
    GROUP BY t.id, t.title, t.created_at, t.updated_at
    -- Last activity first. max(seq) breaks ties, since threads touched in the
    -- same transaction share an updated_at (see the seq column comment).
    ORDER BY t.updated_at DESC, max(m.seq) DESC NULLS LAST
    LIMIT GREATEST(COALESCE(p_limit, 50), 1);
END;
$$;

REVOKE ALL ON FUNCTION public.list_chat_threads(uuid, integer)
  FROM PUBLIC, anon, authenticated;

-- ---------------------------------------------------------------------------
-- delete_chat_thread: soft-delete a conversation and its turns
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.delete_chat_thread(
  p_user_id   uuid,
  p_thread_id uuid
)
RETURNS TABLE (id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
#variable_conflict use_column
DECLARE
  v_thread_id uuid;
BEGIN
  UPDATE public.chat_threads t
     SET deleted_at = now()
   WHERE t.id = p_thread_id
     AND t.user_id = p_user_id
     AND t.deleted_at IS NULL
  RETURNING t.id INTO v_thread_id;

  IF v_thread_id IS NULL THEN
    RETURN;
  END IF;

  -- Stamp the turns too. list_chat_messages already hides them via the thread,
  -- but deleting a conversation should mark the messages themselves deleted so
  -- the privacy export and any future retention sweep see one consistent state.
  UPDATE public.chat_messages m
     SET deleted_at = now()
   WHERE m.thread_id = v_thread_id
     AND m.deleted_at IS NULL;

  RETURN QUERY SELECT v_thread_id;
END;
$$;

REVOKE ALL ON FUNCTION public.delete_chat_thread(uuid, uuid)
  FROM PUBLIC, anon, authenticated;
