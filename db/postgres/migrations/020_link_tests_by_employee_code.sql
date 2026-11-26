-- The employee id (employee_code) is the key that links a test to a person.
-- Backfill rows created without it and index the lookup.

UPDATE public.tests t
SET employee_code = e.employee_id
FROM public.employees e
WHERE e.id = t.employee_id AND btrim(t.employee_code) = '';

CREATE INDEX IF NOT EXISTS ix_tests_client_employee_code ON public.tests (client_id, employee_code);
