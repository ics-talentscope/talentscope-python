-- Correct audit/system log rows written before login routes scoped their audit
-- writes to the actor's email domain.

UPDATE public.audit_logs
SET client_id = 'slalom'
WHERE client_id = 'infinite' AND LOWER(actor_email) LIKE '%@slalom.com';

UPDATE public.system_logs
SET client_id = 'slalom'
WHERE client_id = 'infinite' AND LOWER(user_email) LIKE '%@slalom.com';
