IF COL_LENGTH(N'dbo.employees', N'email_verified') IS NULL
  ALTER TABLE dbo.employees ADD email_verified BIT NOT NULL
    CONSTRAINT DF_employees_email_verified DEFAULT (0);
GO

IF COL_LENGTH(N'dbo.employee_auth_otps', N'email') IS NULL
  ALTER TABLE dbo.employee_auth_otps ADD email NVARCHAR(254) NULL;
GO
