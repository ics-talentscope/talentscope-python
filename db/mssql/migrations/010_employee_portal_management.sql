IF COL_LENGTH(N'dbo.employees', N'designation') IS NULL
  ALTER TABLE dbo.employees ADD designation NVARCHAR(200) NOT NULL CONSTRAINT DF_employees_designation DEFAULT (N'');
GO

IF OBJECT_ID(N'dbo.employee_portal_question_bank', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.employee_portal_question_bank (
    id                   UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_employee_portal_question_bank PRIMARY KEY,
    bank_version         UNIQUEIDENTIFIER NOT NULL,
    product_key          NVARCHAR(200) NOT NULL,
    question_text        NVARCHAR(MAX) NOT NULL,
    options              NVARCHAR(MAX) NOT NULL,
    correct_option_index INT NOT NULL,
    explanation          NVARCHAR(MAX) NOT NULL CONSTRAINT DF_epqb_explanation DEFAULT (N''),
    difficulty           NVARCHAR(32) NOT NULL CONSTRAINT DF_epqb_difficulty DEFAULT (N'medium'),
    created_at           DATETIMEOFFSET NOT NULL CONSTRAINT DF_epqb_created_at DEFAULT (SYSUTCDATETIME())
  );
  CREATE INDEX IX_epqb_product_version ON dbo.employee_portal_question_bank (product_key, bank_version);
END;
GO

IF OBJECT_ID(N'dbo.employee_auth_otps', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.employee_auth_otps (
    id          UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_employee_auth_otps PRIMARY KEY,
    employee_id UNIQUEIDENTIFIER NOT NULL,
    purpose     NVARCHAR(32) NOT NULL,
    code_hash   NVARCHAR(64) NOT NULL,
    expires_at  DATETIMEOFFSET NOT NULL,
    attempts    INT NOT NULL CONSTRAINT DF_employee_auth_otps_attempts DEFAULT (0),
    consumed    BIT NOT NULL CONSTRAINT DF_employee_auth_otps_consumed DEFAULT (0),
    created_at  DATETIMEOFFSET NOT NULL CONSTRAINT DF_employee_auth_otps_created_at DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT FK_employee_auth_otps_employee FOREIGN KEY (employee_id) REFERENCES dbo.employees(id) ON DELETE CASCADE
  );
  CREATE INDEX IX_employee_auth_otps_lookup ON dbo.employee_auth_otps (employee_id, purpose, created_at);
END;
GO
