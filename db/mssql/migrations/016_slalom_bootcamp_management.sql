-- Slalom bootcamp management: tenant-scoped assessment, cohort, assignment and
-- learning-content configuration. Attempt delivery stays in tests /
-- test_questions / test_attempts; these tables only describe the bootcamp.

IF OBJECT_ID(N'dbo.bootcamp_assessments', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.bootcamp_assessments (
    id                   UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_bootcamp_assessments PRIMARY KEY,
    client_id            NVARCHAR(64) NOT NULL CONSTRAINT DF_bootcamp_assessments_client_id DEFAULT (N'infinite'),
    title                NVARCHAR(200) NOT NULL,
    description          NVARCHAR(MAX) NULL,
    topic_id             NVARCHAR(200) NOT NULL,
    question_count       INT NOT NULL CONSTRAINT DF_bootcamp_assessments_question_count DEFAULT (10),
    time_limit_seconds   INT NOT NULL CONSTRAINT DF_bootcamp_assessments_time_limit DEFAULT (1800),
    passing_score        INT NOT NULL CONSTRAINT DF_bootcamp_assessments_passing_score DEFAULT (0),
    question_bank_product NVARCHAR(200) NULL,
    subject_id           NVARCHAR(200) NULL,
    subject_title        NVARCHAR(500) NULL,
    difficulty           NVARCHAR(32) NOT NULL CONSTRAINT DF_bootcamp_assessments_difficulty DEFAULT (N'medium'),
    question_source      NVARCHAR(32) NOT NULL CONSTRAINT DF_bootcamp_assessments_question_source DEFAULT (N'bank'),
    config               NVARCHAR(MAX) NULL,
    is_active            BIT NOT NULL CONSTRAINT DF_bootcamp_assessments_is_active DEFAULT (1),
    created_at           DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_assessments_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at           DATETIMEOFFSET NULL,
    created_by           NVARCHAR(254) NOT NULL CONSTRAINT DF_bootcamp_assessments_created_by DEFAULT (N'')
  );
  CREATE INDEX IX_bootcamp_assessments_client ON dbo.bootcamp_assessments (client_id);
END;
GO

IF OBJECT_ID(N'dbo.bootcamp_cohorts', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.bootcamp_cohorts (
    id          UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_bootcamp_cohorts PRIMARY KEY,
    client_id   NVARCHAR(64) NOT NULL CONSTRAINT DF_bootcamp_cohorts_client_id DEFAULT (N'infinite'),
    name        NVARCHAR(200) NOT NULL,
    description NVARCHAR(MAX) NULL,
    is_active   BIT NOT NULL CONSTRAINT DF_bootcamp_cohorts_is_active DEFAULT (1),
    created_at  DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_cohorts_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at  DATETIMEOFFSET NULL,
    created_by  NVARCHAR(254) NOT NULL CONSTRAINT DF_bootcamp_cohorts_created_by DEFAULT (N'')
  );
  CREATE INDEX IX_bootcamp_cohorts_client ON dbo.bootcamp_cohorts (client_id);
END;
GO

IF OBJECT_ID(N'dbo.bootcamp_cohort_members', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.bootcamp_cohort_members (
    id             UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_bootcamp_cohort_members PRIMARY KEY,
    client_id      NVARCHAR(64) NOT NULL CONSTRAINT DF_bootcamp_cohort_members_client_id DEFAULT (N'infinite'),
    cohort_id      UNIQUEIDENTIFIER NOT NULL,
    employee_id    NVARCHAR(64) NOT NULL,
    employee_uuid  NVARCHAR(64) NULL,
    employee_name  NVARCHAR(300) NULL,
    employee_email NVARCHAR(320) NULL,
    joined_at      DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_cohort_members_joined_at DEFAULT (SYSUTCDATETIME()),
    created_at     DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_cohort_members_created_at DEFAULT (SYSUTCDATETIME()),
    created_by     NVARCHAR(254) NOT NULL CONSTRAINT DF_bootcamp_cohort_members_created_by DEFAULT (N'')
  );
  CREATE UNIQUE INDEX UX_bootcamp_cohort_member ON dbo.bootcamp_cohort_members (client_id, cohort_id, employee_id);
  CREATE INDEX IX_bootcamp_cohort_members_client ON dbo.bootcamp_cohort_members (client_id);
END;
GO

IF COL_LENGTH(N'dbo.bootcamp_cohort_members', N'employee_name') IS NULL
BEGIN
  ALTER TABLE dbo.bootcamp_cohort_members ADD employee_name NVARCHAR(300) NULL;
END;
GO

IF OBJECT_ID(N'dbo.bootcamp_assignments', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.bootcamp_assignments (
    id            UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_bootcamp_assignments PRIMARY KEY,
    client_id     NVARCHAR(64) NOT NULL CONSTRAINT DF_bootcamp_assignments_client_id DEFAULT (N'infinite'),
    cohort_id     UNIQUEIDENTIFIER NULL,
    assessment_id UNIQUEIDENTIFIER NOT NULL,
    employee_id   NVARCHAR(64) NOT NULL,
    test_id       UNIQUEIDENTIFIER NULL,
    status        NVARCHAR(32) NOT NULL CONSTRAINT DF_bootcamp_assignments_status DEFAULT (N'assigned'),
    assigned_at   DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_assignments_assigned_at DEFAULT (SYSUTCDATETIME()),
    created_at    DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_assignments_created_at DEFAULT (SYSUTCDATETIME()),
    created_by    NVARCHAR(254) NOT NULL CONSTRAINT DF_bootcamp_assignments_created_by DEFAULT (N'')
  );
  CREATE UNIQUE INDEX UX_bootcamp_assignment ON dbo.bootcamp_assignments (client_id, assessment_id, employee_id);
  CREATE INDEX IX_bootcamp_assignments_client ON dbo.bootcamp_assignments (client_id);
END;
GO

IF OBJECT_ID(N'dbo.bootcamp_content', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.bootcamp_content (
    id           UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_bootcamp_content PRIMARY KEY,
    client_id    NVARCHAR(64) NOT NULL CONSTRAINT DF_bootcamp_content_client_id DEFAULT (N'infinite'),
    content_type NVARCHAR(32) NOT NULL,
    content_key  NVARCHAR(200) NOT NULL,
    title        NVARCHAR(500) NULL,
    payload      NVARCHAR(MAX) NULL,
    is_active    BIT NOT NULL CONSTRAINT DF_bootcamp_content_is_active DEFAULT (1),
    created_at   DATETIMEOFFSET NOT NULL CONSTRAINT DF_bootcamp_content_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at   DATETIMEOFFSET NULL,
    created_by   NVARCHAR(254) NOT NULL CONSTRAINT DF_bootcamp_content_created_by DEFAULT (N'')
  );
  CREATE UNIQUE INDEX UX_bootcamp_content_key ON dbo.bootcamp_content (client_id, content_type, content_key);
  CREATE INDEX IX_bootcamp_content_client ON dbo.bootcamp_content (client_id);
END;
GO
