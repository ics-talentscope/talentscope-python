IF OBJECT_ID(N'dbo.system_logs', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.system_logs (
    id                UNIQUEIDENTIFIER NOT NULL CONSTRAINT PK_system_logs PRIMARY KEY,
    [timestamp]       DATETIMEOFFSET NOT NULL,
    log_level         NVARCHAR(16) NOT NULL,
    module            NVARCHAR(80) NOT NULL,
    action            NVARCHAR(120) NOT NULL,
    user_email        NVARCHAR(320) NULL,
    user_role         NVARCHAR(80) NULL,
    ip_address        NVARCHAR(64) NULL,
    user_agent        NVARCHAR(512) NULL,
    request_id        NVARCHAR(128) NULL,
    endpoint          NVARCHAR(512) NULL,
    db_operation      NVARCHAR(16) NULL,
    table_name        NVARCHAR(128) NULL,
    record_id         NVARCHAR(128) NULL,
    model             NVARCHAR(120) NULL,
    execution_time    DECIMAL(12,2) NULL,
    input_tokens      INT NULL,
    output_tokens     INT NULL,
    status            NVARCHAR(32) NOT NULL,
    message           NVARCHAR(2000) NOT NULL,
    error_message     NVARCHAR(2000) NULL,
    stack_trace       NVARCHAR(MAX) NULL,
    request_payload   NVARCHAR(MAX) NULL,
    response_payload  NVARCHAR(MAX) NULL
  );
  CREATE INDEX IX_system_logs_timestamp ON dbo.system_logs ([timestamp] DESC);
  CREATE INDEX IX_system_logs_request_id ON dbo.system_logs (request_id) WHERE request_id IS NOT NULL;
  CREATE INDEX IX_system_logs_module_level ON dbo.system_logs (module, log_level, [timestamp] DESC);
END;
