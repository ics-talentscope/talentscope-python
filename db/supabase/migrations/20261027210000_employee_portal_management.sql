ALTER TABLE public.employees ADD COLUMN IF NOT EXISTS designation VARCHAR(200) NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS public.employee_portal_question_bank (
  id                   UUID PRIMARY KEY,
  bank_version         UUID NOT NULL,
  product_key          VARCHAR(200) NOT NULL,
  question_text        TEXT NOT NULL,
  options              JSONB NOT NULL,
  correct_option_index INTEGER NOT NULL,
  explanation          TEXT NOT NULL DEFAULT '',
  difficulty           VARCHAR(32) NOT NULL DEFAULT 'medium',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS IX_epqb_product_version
  ON public.employee_portal_question_bank (product_key, bank_version);

CREATE TABLE IF NOT EXISTS public.employee_auth_otps (
  id          UUID PRIMARY KEY,
  employee_id UUID NOT NULL REFERENCES public.employees(id) ON DELETE CASCADE,
  purpose     VARCHAR(32) NOT NULL,
  code_hash   VARCHAR(64) NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  consumed    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS IX_employee_auth_otps_lookup
  ON public.employee_auth_otps (employee_id, purpose, created_at);
