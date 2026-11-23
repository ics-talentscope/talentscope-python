-- Optional topic tagging for employee question-bank rows. Slalom bootcamp
-- assessments use these tags to honour the admin-selected topics.

ALTER TABLE public.employee_portal_question_bank
  ADD COLUMN IF NOT EXISTS topic VARCHAR(200);
