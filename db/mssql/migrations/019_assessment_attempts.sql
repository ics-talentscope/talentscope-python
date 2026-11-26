-- Assessment attempt management (meeting: one attempt per assessment, no
-- second attempt per question, retake requires administrator authorization).

IF OBJECT_ID(N'dbo.assessment_attempts', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.assessment_attempts (
    id                 UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_assessment_attempts PRIMARY KEY DEFAULT NEWID(),
    client_id          NVARCHAR(64) NOT NULL CONSTRAINT DF_assessment_attempts_client_id DEFAULT (N'infinite'),
    employee_id        NVARCHAR(64) NOT NULL,
    employee_uuid      UNIQUEIDENTIFIER NOT NULL,
    topic_id           NVARCHAR(200) NOT NULL,
    test_id            UNIQUEIDENTIFIER NOT NULL,
    attempt_number     INT NOT NULL CONSTRAINT DF_assessment_attempts_attempt_number DEFAULT (1),
    started_at         DATETIMEOFFSET NULL,
    completed_at       DATETIMEOFFSET NULL,
    status             NVARCHAR(32) NOT NULL CONSTRAINT DF_assessment_attempts_status DEFAULT (N'in_progress'),
    score              DECIMAL(5,2) NULL,
    is_retake_authorized BIT NOT NULL CONSTRAINT DF_assessment_attempts_retake DEFAULT (0),
    authorized_by      NVARCHAR(320) NULL,
    authorized_at      DATETIMEOFFSET NULL,
    created_at         DATETIMEOFFSET NOT NULL CONSTRAINT DF_assessment_attempts_created_at DEFAULT (SYSUTCDATETIME()),
    updated_at         DATETIMEOFFSET NULL,
    CONSTRAINT FK_assessment_attempts_employee FOREIGN KEY (employee_uuid) REFERENCES dbo.employees(id) ON DELETE CASCADE,
    CONSTRAINT FK_assessment_attempts_test FOREIGN KEY (test_id) REFERENCES dbo.tests(id) ON DELETE NO ACTION
  );
  CREATE INDEX IX_assessment_attempts_client ON dbo.assessment_attempts (client_id);
  CREATE INDEX IX_assessment_attempts_employee_topic ON dbo.assessment_attempts (employee_id, topic_id);
  CREATE UNIQUE INDEX UX_assessment_attempts_in_progress ON dbo.assessment_attempts (client_id, employee_id, topic_id)
    WHERE status = N'in_progress';
END;
GO
