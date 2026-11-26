-- Assessment scoring enhancements (meeting: configurable question marks,
-- weighted pass/fail scoring with an 80% default threshold).
-- Add configurable marks to each question and a pass threshold to each test.

ALTER TABLE public.test_questions
  ADD COLUMN IF NOT EXISTS marks INT NOT NULL DEFAULT 2;

ALTER TABLE public.tests
  ADD COLUMN IF NOT EXISTS pass_threshold INT NOT NULL DEFAULT 80;

ALTER TABLE public.tests
  ADD COLUMN IF NOT EXISTS total_marks INT NULL;

ALTER TABLE public.tests
  ADD COLUMN IF NOT EXISTS correct_marks INT NULL;

ALTER TABLE public.tests
  ADD COLUMN IF NOT EXISTS passed BOOLEAN NULL;
