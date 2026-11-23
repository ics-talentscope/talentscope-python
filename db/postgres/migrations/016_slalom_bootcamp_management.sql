-- Slalom bootcamp management configuration. All rows are client-owned so
-- Infinite and Slalom bootcamp data cannot leak into each other.

CREATE TABLE IF NOT EXISTS public.bootcamp_assessments (
  id                    UUID PRIMARY KEY,
  client_id             VARCHAR(64) NOT NULL DEFAULT 'infinite',
  title                 VARCHAR(200) NOT NULL,
  description           TEXT,
  topic_id              VARCHAR(200) NOT NULL,
  question_count        INTEGER NOT NULL DEFAULT 10,
  time_limit_seconds    INTEGER NOT NULL DEFAULT 1800,
  passing_score         INTEGER NOT NULL DEFAULT 0,
  question_bank_product VARCHAR(200),
  subject_id            VARCHAR(200),
  subject_title         VARCHAR(500),
  difficulty            VARCHAR(32) NOT NULL DEFAULT 'medium',
  question_source       VARCHAR(32) NOT NULL DEFAULT 'bank',
  config                JSONB,
  is_active             BOOLEAN NOT NULL DEFAULT TRUE,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at            TIMESTAMPTZ,
  created_by            VARCHAR(254) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bootcamp_assessments_client ON public.bootcamp_assessments (client_id);

CREATE TABLE IF NOT EXISTS public.bootcamp_cohorts (
  id          UUID PRIMARY KEY,
  client_id   VARCHAR(64) NOT NULL DEFAULT 'infinite',
  name        VARCHAR(200) NOT NULL,
  description TEXT,
  is_active   BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMPTZ,
  created_by  VARCHAR(254) NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_bootcamp_cohorts_client ON public.bootcamp_cohorts (client_id);

CREATE TABLE IF NOT EXISTS public.bootcamp_cohort_members (
  id             UUID PRIMARY KEY,
  client_id      VARCHAR(64) NOT NULL DEFAULT 'infinite',
  cohort_id      UUID NOT NULL,
  employee_id    VARCHAR(64) NOT NULL,
  employee_uuid  VARCHAR(64),
  employee_name  VARCHAR(300),
  employee_email VARCHAR(320),
  joined_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by     VARCHAR(254) NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bootcamp_cohort_member
  ON public.bootcamp_cohort_members (client_id, cohort_id, employee_id);
CREATE INDEX IF NOT EXISTS ix_bootcamp_cohort_members_client ON public.bootcamp_cohort_members (client_id);
ALTER TABLE public.bootcamp_cohort_members ADD COLUMN IF NOT EXISTS employee_name VARCHAR(300);

CREATE TABLE IF NOT EXISTS public.bootcamp_assignments (
  id            UUID PRIMARY KEY,
  client_id     VARCHAR(64) NOT NULL DEFAULT 'infinite',
  cohort_id     UUID,
  assessment_id UUID NOT NULL,
  employee_id   VARCHAR(64) NOT NULL,
  test_id       UUID,
  status        VARCHAR(32) NOT NULL DEFAULT 'assigned',
  assigned_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by    VARCHAR(254) NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bootcamp_assignment
  ON public.bootcamp_assignments (client_id, assessment_id, employee_id);
CREATE INDEX IF NOT EXISTS ix_bootcamp_assignments_client ON public.bootcamp_assignments (client_id);

CREATE TABLE IF NOT EXISTS public.bootcamp_content (
  id           UUID PRIMARY KEY,
  client_id    VARCHAR(64) NOT NULL DEFAULT 'infinite',
  content_type VARCHAR(32) NOT NULL,
  content_key  VARCHAR(200) NOT NULL,
  title        VARCHAR(500),
  payload      JSONB,
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMPTZ,
  created_by   VARCHAR(254) NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bootcamp_content_key
  ON public.bootcamp_content (client_id, content_type, content_key);
CREATE INDEX IF NOT EXISTS ix_bootcamp_content_client ON public.bootcamp_content (client_id);
