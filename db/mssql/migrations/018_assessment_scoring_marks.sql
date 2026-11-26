-- Assessment scoring enhancements (meeting: configurable question marks,
-- weighted pass/fail scoring with an 80% default threshold).
-- Add configurable marks to each question and a pass threshold to each test.

IF COL_LENGTH(N'dbo.test_questions', N'marks') IS NULL
  ALTER TABLE dbo.test_questions ADD marks INT NOT NULL
    CONSTRAINT DF_test_questions_marks DEFAULT (2);
GO

IF COL_LENGTH(N'dbo.tests', N'pass_threshold') IS NULL
  ALTER TABLE dbo.tests ADD pass_threshold INT NOT NULL
    CONSTRAINT DF_tests_pass_threshold DEFAULT (80);
GO

IF COL_LENGTH(N'dbo.tests', N'total_marks') IS NULL
  ALTER TABLE dbo.tests ADD total_marks INT NULL;
GO

IF COL_LENGTH(N'dbo.tests', N'correct_marks') IS NULL
  ALTER TABLE dbo.tests ADD correct_marks INT NULL;
GO

IF COL_LENGTH(N'dbo.tests', N'passed') IS NULL
  ALTER TABLE dbo.tests ADD passed BIT NULL;
GO
