-- Rename the secondary client environment from databricks to slalom.
-- Preserve any rows already written under the old client id.

DECLARE @table NVARCHAR(128);
DECLARE @sql NVARCHAR(MAX);
DECLARE client_tables CURSOR LOCAL FAST_FORWARD FOR
  SELECT name FROM (VALUES
    (N'resumes'), (N'job_descriptions'), (N'portal_settings'), (N'employees'), (N'tests'), (N'audit_logs'),
    (N'simulated_emails'), (N'candidate_interview_data'), (N'interview_questions'), (N'interview_attempts'),
    (N'candidate_sessions'), (N'reset_logs'), (N'learning_subjects'), (N'learning_modules'), (N'learning_topics'),
    (N'learning_resources'), (N'test_questions'), (N'test_attempts'), (N'evaluations'), (N'behavior_evaluations'),
    (N'business_impacts'), (N'system_logs'), (N'storage_objects'), (N'question_sets'), (N'interview_question_bank'),
    (N'employee_portal_question_bank'), (N'employee_auth_otps')
  ) AS v(name);

OPEN client_tables;
FETCH NEXT FROM client_tables INTO @table;
WHILE @@FETCH_STATUS = 0
BEGIN
  IF COL_LENGTH(N'dbo.' + @table, N'client_id') IS NOT NULL
  BEGIN
    SET @sql = N'UPDATE dbo.' + QUOTENAME(@table) +
               N' SET client_id = N''slalom'' WHERE client_id = N''databricks'';';
    EXEC sp_executesql @sql;
  END
  FETCH NEXT FROM client_tables INTO @table;
END
CLOSE client_tables;
DEALLOCATE client_tables;
GO
