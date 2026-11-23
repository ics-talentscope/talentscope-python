-- Client isolation: every application row belongs to one client environment.
-- Existing rows are backfilled to the legacy/default client ("infinite").
-- The application stamps client_id on insert and filters every query by it.

IF COL_LENGTH(N'dbo.resumes', N'client_id') IS NULL
  ALTER TABLE dbo.resumes ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_resumes_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.job_descriptions', N'client_id') IS NULL
  ALTER TABLE dbo.job_descriptions ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_job_descriptions_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.portal_settings', N'client_id') IS NULL
  ALTER TABLE dbo.portal_settings ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_portal_settings_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.employees', N'client_id') IS NULL
  ALTER TABLE dbo.employees ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_employees_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.tests', N'client_id') IS NULL
  ALTER TABLE dbo.tests ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_tests_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.audit_logs', N'client_id') IS NULL
  ALTER TABLE dbo.audit_logs ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_audit_logs_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.simulated_emails', N'client_id') IS NULL
  ALTER TABLE dbo.simulated_emails ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_simulated_emails_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.candidate_interview_data', N'client_id') IS NULL
  ALTER TABLE dbo.candidate_interview_data ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_candidate_interview_data_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.interview_questions', N'client_id') IS NULL
  ALTER TABLE dbo.interview_questions ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_interview_questions_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.interview_attempts', N'client_id') IS NULL
  ALTER TABLE dbo.interview_attempts ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_interview_attempts_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.candidate_sessions', N'client_id') IS NULL
  ALTER TABLE dbo.candidate_sessions ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_candidate_sessions_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.reset_logs', N'client_id') IS NULL
  ALTER TABLE dbo.reset_logs ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_reset_logs_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.learning_subjects', N'client_id') IS NULL
  ALTER TABLE dbo.learning_subjects ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_learning_subjects_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.learning_modules', N'client_id') IS NULL
  ALTER TABLE dbo.learning_modules ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_learning_modules_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.learning_topics', N'client_id') IS NULL
  ALTER TABLE dbo.learning_topics ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_learning_topics_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.learning_resources', N'client_id') IS NULL
  ALTER TABLE dbo.learning_resources ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_learning_resources_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.test_questions', N'client_id') IS NULL
  ALTER TABLE dbo.test_questions ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_test_questions_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.test_attempts', N'client_id') IS NULL
  ALTER TABLE dbo.test_attempts ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_test_attempts_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.evaluations', N'client_id') IS NULL
  ALTER TABLE dbo.evaluations ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_evaluations_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.behavior_evaluations', N'client_id') IS NULL
  ALTER TABLE dbo.behavior_evaluations ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_behavior_evaluations_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.business_impacts', N'client_id') IS NULL
  ALTER TABLE dbo.business_impacts ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_business_impacts_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.system_logs', N'client_id') IS NULL
  ALTER TABLE dbo.system_logs ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_system_logs_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.storage_objects', N'client_id') IS NULL
  ALTER TABLE dbo.storage_objects ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_storage_objects_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.question_sets', N'client_id') IS NULL
  ALTER TABLE dbo.question_sets ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_question_sets_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.interview_question_bank', N'client_id') IS NULL
  ALTER TABLE dbo.interview_question_bank ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_interview_question_bank_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.employee_portal_question_bank', N'client_id') IS NULL
  ALTER TABLE dbo.employee_portal_question_bank ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_employee_portal_question_bank_client_id DEFAULT (N'infinite');
GO
IF COL_LENGTH(N'dbo.employee_auth_otps', N'client_id') IS NULL
  ALTER TABLE dbo.employee_auth_otps ADD client_id NVARCHAR(64) NOT NULL CONSTRAINT DF_employee_auth_otps_client_id DEFAULT (N'infinite');
GO

-- Keys that must be unique per client, not globally.
ALTER TABLE dbo.portal_settings DROP CONSTRAINT IF EXISTS PK_portal_settings;
GO
ALTER TABLE dbo.portal_settings ADD CONSTRAINT PK_portal_settings PRIMARY KEY (client_id, [key]);
GO
ALTER TABLE dbo.employees DROP CONSTRAINT IF EXISTS UQ_employees_employee_id;
GO
ALTER TABLE dbo.employees ADD CONSTRAINT UQ_employees_employee_id UNIQUE (client_id, employee_id);
GO
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_cs_code' AND object_id = OBJECT_ID(N'dbo.candidate_sessions'))
  DROP INDEX UX_cs_code ON dbo.candidate_sessions;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_cs_code' AND object_id = OBJECT_ID(N'dbo.candidate_sessions'))
  CREATE UNIQUE INDEX UX_cs_code ON dbo.candidate_sessions (client_id, code) WHERE code IS NOT NULL;
GO
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_cs_email' AND object_id = OBJECT_ID(N'dbo.candidate_sessions'))
  DROP INDEX UX_cs_email ON dbo.candidate_sessions;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'UX_cs_email' AND object_id = OBJECT_ID(N'dbo.candidate_sessions'))
  CREATE UNIQUE INDEX UX_cs_email ON dbo.candidate_sessions (client_id, email) WHERE email IS NOT NULL;
GO
ALTER TABLE dbo.tests DROP CONSTRAINT IF EXISTS UQ_tests_employee_topic;
GO
ALTER TABLE dbo.tests ADD CONSTRAINT UQ_tests_employee_topic UNIQUE (client_id, employee_id, topic_id);
GO
ALTER TABLE dbo.test_questions DROP CONSTRAINT IF EXISTS UQ_tq_test_index;
GO
ALTER TABLE dbo.test_questions ADD CONSTRAINT UQ_tq_test_index UNIQUE (client_id, test_id, question_index);
GO
ALTER TABLE dbo.evaluations DROP CONSTRAINT IF EXISTS UQ_evaluations_emp_subject;
GO
ALTER TABLE dbo.evaluations ADD CONSTRAINT UQ_evaluations_emp_subject UNIQUE (client_id, employee_id, subject_id);
GO
ALTER TABLE dbo.behavior_evaluations DROP CONSTRAINT IF EXISTS UQ_behavior_eval;
GO
ALTER TABLE dbo.behavior_evaluations ADD CONSTRAINT UQ_behavior_eval UNIQUE (client_id, employee_id, subject_id, evaluator_role, interval_days);
GO
ALTER TABLE dbo.business_impacts DROP CONSTRAINT IF EXISTS UQ_business_impacts;
GO
ALTER TABLE dbo.business_impacts ADD CONSTRAINT UQ_business_impacts UNIQUE (client_id, employee_id, subject_id);
GO

IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_storage_objects_bucket_path' AND object_id = OBJECT_ID(N'dbo.storage_objects'))
  DROP INDEX UX_storage_objects_bucket_path ON dbo.storage_objects;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_storage_objects_bucket_path' AND object_id = OBJECT_ID(N'dbo.storage_objects'))
  CREATE UNIQUE NONCLUSTERED INDEX UX_storage_objects_bucket_path
    ON dbo.storage_objects (client_id, bucket, path) INCLUDE (size_bytes, content_type, created_at, updated_at);
GO
IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_iqb_key_hash' AND object_id = OBJECT_ID(N'dbo.interview_question_bank'))
  DROP INDEX UX_iqb_key_hash ON dbo.interview_question_bank;
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_iqb_key_hash' AND object_id = OBJECT_ID(N'dbo.interview_question_bank'))
  CREATE UNIQUE INDEX UX_iqb_key_hash ON dbo.interview_question_bank (client_id, bank_key, kind, text_hash);
GO

-- Cover the client predicate used by every read/write.
DECLARE @table NVARCHAR(128);
DECLARE @index NVARCHAR(200);
DECLARE @sql NVARCHAR(MAX);
DECLARE client_tables CURSOR LOCAL FAST_FORWARD FOR
  SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID(N'dbo') AND name <> '__migrations' AND name <> '__live_sync_rows';
OPEN client_tables;
FETCH NEXT FROM client_tables INTO @table;
WHILE @@FETCH_STATUS = 0
BEGIN
  SET @index = N'IX_' + @table + N'_client';
  IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = @index AND object_id = OBJECT_ID(N'dbo.' + QUOTENAME(@table)))
  BEGIN
    SET @sql = N'CREATE INDEX ' + QUOTENAME(@index) + N' ON dbo.' + QUOTENAME(@table) + N' (client_id);';
    EXEC sp_executesql @sql;
  END
  FETCH NEXT FROM client_tables INTO @table;
END
CLOSE client_tables;
DEALLOCATE client_tables;
GO
