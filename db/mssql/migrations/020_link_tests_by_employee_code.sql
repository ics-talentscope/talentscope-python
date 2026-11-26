-- The employee id (employee_code) is the key that links a test to a person.
-- Backfill rows created without it and index the lookup.

UPDATE t
SET t.employee_code = e.employee_id
FROM dbo.tests t
JOIN dbo.employees e ON e.id = t.employee_id
WHERE LTRIM(RTRIM(t.employee_code)) = N'';

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'IX_tests_client_employee_code' AND object_id = OBJECT_ID(N'dbo.tests'))
  CREATE INDEX IX_tests_client_employee_code ON dbo.tests (client_id, employee_code);
