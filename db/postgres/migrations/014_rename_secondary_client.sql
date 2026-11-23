-- Rename the secondary client environment from databricks to slalom.
-- Preserve any rows already written under the old client id.

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
        'UPDATE public.%I SET client_id = %L WHERE client_id = %L',
        tbl, 'slalom', 'databricks'
      );
    END IF;
  END LOOP;
END $$;
