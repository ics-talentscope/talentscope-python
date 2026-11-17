ALTER TABLE public.candidate_sessions
  ADD COLUMN IF NOT EXISTS password_hash VARCHAR(128),
  ADD COLUMN IF NOT EXISTS password_salt VARCHAR(64);
