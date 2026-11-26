-- Assessment attempt management (meeting: one attempt per assessment, no
-- second attempt per question, retake requires administrator authorization).
-- This table tracks attempts at the assessment level on top of the existing
-- tests / test_questions / test_attempts delivery tables.

CREATE TABLE IF NOT EXISTS public.assessment_attempts (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id          VARCHAR(64) NOT NULL DEFAULT 'infinite',
  employee_id        VARCHAR(64) NOT NULL,
  employee_uuid      UUID NOT NULL,
  topic_id           VARCHAR(200) NOT NULL,
  test_id            UUID NOT NULL,
  attempt_number     INT NOT NULL DEFAULT 1,
  started_at         TIMESTAMPTZ NULL,
  completed_at       TIMESTAMPTZ NULL,
  status             VARCHAR(32) NOT NULL DEFAULT 'in_progress',
  score              DECIMAL(5,2) NULL,
  is_retake_authorized BOOLEAN NOT NULL DEFAULT FALSE,
  authorized_by      VARCHAR(320) NULL,
  authorized_at      TIMESTAMPTZ NULL,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         TIMESTAMPTZ NULL,
  CONSTRAINT FK_assessment_attempts_employee FOREIGN KEY (employee_uuid) REFERENCES public.employees(id) ON DELETE CASCADE,
  CONSTRAINT FK_assessment_attempts_test FOREIGN KEY (test_id) REFERENCES public.tests(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_assessment_attempts_client ON public.assessment_attempts (client_id);
CREATE INDEX IF NOT EXISTS ix_assessment_attempts_employee_topic ON public.assessment_attempts (employee_id, topic_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_assessment_attempts_in_progress
  ON public.assessment_attempts (client_id, employee_id, topic_id)
  WHERE status = 'in_progress';
