-- Correct audit/system log rows written before login routes scoped their audit
-- writes to the actor's email domain. Those rows were stamped with the default
-- (infinite) client, leaking Slalom sign-in events into Infinite's audit trail.

UPDATE dbo.audit_logs
SET client_id = N'slalom'
WHERE client_id = N'infinite' AND LOWER(actor_email) LIKE N'%@slalom.com';
GO

UPDATE dbo.system_logs
SET client_id = N'slalom'
WHERE client_id = N'infinite' AND LOWER(user_email) LIKE N'%@slalom.com';
GO
