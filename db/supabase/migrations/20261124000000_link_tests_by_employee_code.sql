-- The employee id (employee_code) is the key that links a test to a person.
-- Backfill rows created without it and index the lookup.

update public.tests t
set employee_code = e.employee_id
from public.employees e
where e.id = t.employee_id and btrim(t.employee_code) = '';

create index if not exists ix_tests_client_employee_code on public.tests (client_id, employee_code);
