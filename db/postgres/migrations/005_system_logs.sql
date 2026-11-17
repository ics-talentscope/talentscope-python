CREATE TABLE IF NOT EXISTS public.system_logs (
    id UUID PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL,
    log_level VARCHAR(16) NOT NULL,
    module VARCHAR(80) NOT NULL,
    action VARCHAR(120) NOT NULL,
    user_email VARCHAR(320),
    user_role VARCHAR(80),
    ip_address VARCHAR(64),
    user_agent VARCHAR(512),
    request_id VARCHAR(128),
    endpoint VARCHAR(512),
    db_operation VARCHAR(16),
    table_name VARCHAR(128),
    record_id VARCHAR(128),
    model VARCHAR(120),
    execution_time NUMERIC(12, 2),
    input_tokens INTEGER,
    output_tokens INTEGER,
    status VARCHAR(32) NOT NULL,
    message VARCHAR(2000) NOT NULL,
    error_message VARCHAR(2000),
    stack_trace TEXT,
    request_payload JSONB,
    response_payload JSONB
);

CREATE INDEX IF NOT EXISTS ix_system_logs_timestamp ON public.system_logs ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS ix_system_logs_request_id ON public.system_logs (request_id);
CREATE INDEX IF NOT EXISTS ix_system_logs_module_level ON public.system_logs (module, log_level, "timestamp" DESC);

ALTER TABLE public.system_logs ENABLE ROW LEVEL SECURITY;
