-- Client isolation for the optional Supabase/PostgreSQL runtime.
-- Existing rows are backfilled to the legacy/default client ("infinite").

DO $$
DECLARE
  tbl text;
BEGIN
  FOREACH tbl IN ARRAY ARRAY[
    'resumes', 'job_descriptions', 'portal_settings', 'employees', 'tests', 'audit_logs',
    'simulated_emails', 'candidate_interview_data', 'interview_questions', 'interview_attempts',
    'candidate_sessions', 'reset_logs', 'learning_subjects', 'learning_modules', 'learning_topics',
    'learning_resources', 'test_questions', 'test_attempts', 'evaluations', 'behavior_evaluations',
    'business_impacts', 'system_logs', 'storage_objects', 'question_sets', 'interview_question_bank',
    'employee_portal_question_bank', 'employee_auth_otps'
  ]
  LOOP
    IF to_regclass('public.' || tbl) IS NOT NULL THEN
      EXECUTE format(
        'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS client_id VARCHAR(64) NOT NULL DEFAULT ''infinite''',
        tbl
      );
      EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON public.%I (client_id)', 'ix_' || tbl || '_client', tbl);
    END IF;
  END LOOP;
END $$;

DO $$
DECLARE
  pk_name text;
BEGIN
  SELECT c.conname INTO pk_name
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  JOIN pg_namespace n ON n.oid = t.relnamespace
  WHERE n.nspname = 'public' AND t.relname = 'portal_settings' AND c.contype = 'p';
  IF pk_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE public.portal_settings DROP CONSTRAINT %I', pk_name);
  END IF;
  ALTER TABLE public.portal_settings ADD CONSTRAINT pk_portal_settings PRIMARY KEY (client_id, "key");
END $$;

DO $$
DECLARE
  item record;
  con_name text;
  old_nums smallint[];
BEGIN
  FOR item IN
    SELECT * FROM (VALUES
      ('employees', ARRAY['employee_id'], 'uq_employees_employee_id', ARRAY['client_id', 'employee_id']),
      ('tests', ARRAY['employee_id', 'topic_id'], 'uq_tests_employee_topic', ARRAY['client_id', 'employee_id', 'topic_id']),
      ('test_questions', ARRAY['test_id', 'question_index'], 'uq_tq_test_index', ARRAY['client_id', 'test_id', 'question_index']),
      ('evaluations', ARRAY['employee_id', 'subject_id'], 'uq_evaluations_emp_subject', ARRAY['client_id', 'employee_id', 'subject_id']),
      ('behavior_evaluations', ARRAY['employee_id', 'subject_id', 'evaluator_role', 'interval_days'], 'uq_behavior_eval', ARRAY['client_id', 'employee_id', 'subject_id', 'evaluator_role', 'interval_days']),
      ('business_impacts', ARRAY['employee_id', 'subject_id'], 'uq_business_impacts', ARRAY['client_id', 'employee_id', 'subject_id'])
    ) AS v(tbl, old_cols, new_name, new_cols)
  LOOP
    SELECT array_agg(a.attnum ORDER BY c.ordinality) INTO old_nums
    FROM unnest(item.old_cols) WITH ORDINALITY AS c(col, ordinality)
    JOIN pg_attribute a
      ON a.attrelid = ('public.' || item.tbl)::regclass AND a.attname = c.col;

    FOR con_name IN
      SELECT c.conname
      FROM pg_constraint c
      WHERE c.conrelid = ('public.' || item.tbl)::regclass
        AND c.contype = 'u' AND c.conkey = old_nums
    LOOP
      EXECUTE format('ALTER TABLE public.%I DROP CONSTRAINT %I', item.tbl, con_name);
    END LOOP;

    IF NOT EXISTS (
      SELECT 1 FROM pg_constraint
      WHERE conrelid = ('public.' || item.tbl)::regclass AND conname = item.new_name
    ) THEN
      EXECUTE format(
        'ALTER TABLE public.%I ADD CONSTRAINT %I UNIQUE (%s)',
        item.tbl, item.new_name, array_to_string(item.new_cols, ', ')
      );
    END IF;
  END LOOP;
END $$;

DROP INDEX IF EXISTS public.uq_cs_code;
DROP INDEX IF EXISTS public.ux_cs_code;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_code ON public.candidate_sessions (client_id, code) WHERE code IS NOT NULL;
DROP INDEX IF EXISTS public.uq_cs_email;
DROP INDEX IF EXISTS public.ux_cs_email;
CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_email ON public.candidate_sessions (client_id, email) WHERE email IS NOT NULL;

DROP INDEX IF EXISTS public.ux_storage_objects_bucket_path;
CREATE UNIQUE INDEX IF NOT EXISTS ux_storage_objects_client_bucket_path
  ON public.storage_objects (client_id, bucket, path);

DROP INDEX IF EXISTS public.ux_iqb_key_hash;
CREATE UNIQUE INDEX IF NOT EXISTS ux_iqb_client_key_hash
  ON public.interview_question_bank (client_id, bank_key, kind, text_hash);
