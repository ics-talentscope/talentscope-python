IF COL_LENGTH(N'dbo.candidate_sessions', N'password_hash') IS NULL
  ALTER TABLE dbo.candidate_sessions ADD password_hash NVARCHAR(128) NULL;
GO
IF COL_LENGTH(N'dbo.candidate_sessions', N'password_salt') IS NULL
  ALTER TABLE dbo.candidate_sessions ADD password_salt NVARCHAR(64) NULL;
GO
