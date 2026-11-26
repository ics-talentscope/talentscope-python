-- Assessment scoring enhancements (meeting: configurable question marks,
-- weighted pass/fail scoring with an 80% default threshold) and assessment
-- attempt management. Mirrors the MSSQL/Postgres migrations for the Supabase
-- API database target.

alter table public.test_questions
  add column if not exists marks integer not null default 2;

alter table public.tests
  add column if not exists pass_threshold integer not null default 80;

alter table public.tests
  add column if not exists total_marks integer null;

alter table public.tests
  add column if not exists correct_marks integer null;

alter table public.tests
  add column if not exists passed boolean null;

create table if not exists public.assessment_attempts (
  id                 uuid primary key default gen_random_uuid(),
  client_id          varchar(64) not null default 'infinite',
  employee_id        varchar(64) not null,
  employee_uuid      uuid not null references public.employees(id) on delete cascade,
  topic_id           varchar(200) not null,
  test_id            uuid not null references public.tests(id) on delete cascade,
  attempt_number     integer not null default 1,
  started_at         timestamptz null,
  completed_at       timestamptz null,
  status             varchar(32) not null default 'in_progress',
  score              numeric(5,2) null,
  is_retake_authorized boolean not null default false,
  authorized_by      varchar(320) null,
  authorized_at      timestamptz null,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz null
);
create index if not exists ix_assessment_attempts_client
  on public.assessment_attempts (client_id);
create index if not exists ix_assessment_attempts_employee_topic
  on public.assessment_attempts (employee_id, topic_id);
create unique index if not exists ux_assessment_attempts_in_progress
  on public.assessment_attempts (client_id, employee_id, topic_id)
  where status = 'in_progress';
