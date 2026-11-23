-- Optional topic tagging for employee question-bank rows. Slalom bootcamp
-- assessments use these tags to honour the admin-selected topics.

IF COL_LENGTH(N'dbo.employee_portal_question_bank', N'topic') IS NULL
BEGIN
  ALTER TABLE dbo.employee_portal_question_bank ADD topic NVARCHAR(200) NULL;
END;
GO
