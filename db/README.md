# TalentScope Database (MS SQL Server)

This directory contains the active **Microsoft SQL Server** (local, remote, or Azure SQL) schema migrations, optional baseline datasets, and deployment scripts, plus isolated **Supabase/PostgreSQL** assets. The API defaults to SQL Server; its optional Supabase adapter is selected only by explicitly setting `DB_TARGET=SUPABASE`. The seed JSON files are excluded from Git and Docker by default because they contain personal data; provide approved datasets privately if you need to seed a database.

---

## 📁 Directory Structure

| Path | Description |
|---|---|
| `mssql/migrations/*.sql` | Ordered SQL Server DDL migrations (tracked in `__migrations`) |
| `mssql/data/*.json` | Baseline table seed datasets (employees, job descriptions, test questions, settings) |
| `scripts/migrate.ts` | Migration runner (applies unapplied `.sql` scripts and verifies checksums) |
| `scripts/deploy-data.ts` | Data deployment engine (inserts or upserts baseline datasets into target DB) |
| `scripts/bootstrap.ts` | Creates the database instance if it does not yet exist |
| `scripts/lib/mssql.ts` | Database connection pool configuration and target resolution |
| `mssql/migrations/005_system_logs.sql` | Structured application/request/database log storage |
| `supabase/config.toml` | Supabase CLI local-development configuration |
| `supabase/migrations/*.sql` | PostgreSQL schema migrations for Supabase |
| `postgres/migrations/*.sql` | Ordered PostgreSQL migrations applied by `scripts/migrate_postgres.py` |

---

## ⚙️ Target Environments

The database tooling connects to the target specified by `MSSQL_TARGET` in the repo root [`.env.local`](../../.env.local) (or `--target=<name>` on CLI):

| Target | Env Prefix | Description |
|---|---|---|
| `local` | `MSSQL_LOCAL_*` | Local MS SQL Server (e.g. `localhost:1433`, default) |
| `azure` | `MSSQL_AZURE_*` | Microsoft Azure SQL Database (TLS encryption enforced) |
| `remote` | `MSSQL_REMOTE_*` | Company staging / dev SQL Server |

---

## 🚀 Running Migrations & Seeding

### 1. Install Dependencies
```powershell
cd python/db
npm install
```

### 2. Check Migration Status
Check which migrations are applied and which are pending:
```powershell
npm run db:status
```

### 3. Apply Schema Migrations
```powershell
npm run db:migrate
```

### 4. Deploy Baseline Datasets
Populate standard test questions, curriculum topics, settings, and baseline employees:
```powershell
# Dry run (test without modifying database)
npm run db:deploy-data -- --dry-run

# Actual deployment
npm run db:deploy-data
```

Seed deployment is additive: it inserts missing primary keys, updates only rows whose
values differ, and leaves identical or unlisted existing rows untouched. It never deletes
rows; the old `--prune` option is rejected.

### 5. Combined One-Step Setup (Local)
Runs both migrations and baseline data deployment against local SQL Server:
```powershell
npm run db:local
```

### Live → Dev sync

`scripts/sync-live-to-dev.ts` copies new and changed rows from `TalentScope_DB_Live` into the app database. It never writes Live and never deletes Dev-only rows. Employee ID is the stable key: when the same employee exists in both databases under different GUIDs, Dev is remapped to Live (employees, then tests, questions, and sessions) and Live’s columns overwrite Dev.

```powershell
# One pass (uses MSSQL_TARGET from .env.local, or pass --target=remote)
npm run db:sync-live

# Preview without committing writes
npm run db:sync-live -- --dry-run

# Repeat on this machine every 15 minutes
npm run db:sync-live:loop
```

Set `SYNC_INTERVAL_MINUTES` or `--interval-minutes=N` for a different cadence. On the development VM, `.github/workflows/sync-live-to-dev.yml` runs the same one-pass job every 15 minutes against `/opt/talentscope-dev/.env.local`.

## Data-preserving migration policy

- Migrations are ordered by filename within their engine-specific directory. SQL Server and
  PostgreSQL use numbered names; Supabase uses timestamp names.
- Applied migration files are immutable. Do not edit or renumber them; the repeated `005`
  prefixes in the existing SQL Server and PostgreSQL histories are retained so migration
  ledgers remain valid. Start new numbered migrations after `012`.
- Migration runners reject pending SQL containing row deletes, truncation, table/column
  drops, or database/schema drops. Additive and conditional schema changes are allowed.
- Bulk provisioning must preserve assessment history. In particular,
  `../scripts/bulk_tpm_upskilling.py` leaves started/completed tests and their attempts
  untouched; for an unstarted pending test it adds only missing question text and never
  deletes or replaces existing questions.
- The migration ledger is part of the history and must not be dropped to bypass a checksum
  mismatch. Restore the original applied file and add a new migration for the change.
- `supabase:reset` is a local development reset, not a migration command; do not use it
  against a database whose data must be retained.

### Application logs

The Python API uses Loguru for terminal output and a single combined daily log file for
all levels. On the Azure VM, production files are written to `/opt/logs/prod/` and
development files to `/opt/logs/dev/`, with names such as `2026-10-30.log`. Each line
uses a 24-hour timestamp. Files are rotated at midnight. At 00:05 on the first day of
each month, the previous month's daily files are verified and bundled into
`archive/YYYY-MM.zip` inside the matching environment folder; source daily files are
removed only after the archive passes verification. The deployment scripts mount the
VM's `/opt/logs` directory into the API container. Local `logs/` files are Git-ignored,
excluded from Docker build contexts, and excluded explicitly from development and
production `rsync` deployment. Deployment therefore updates the API code without
copying local log files to the VM; runtime logs remain in the VM's mounted `/opt/logs`.
Database logs are stored in `dbo.system_logs`; run the normal database migration before
enabling the new log viewer features.
For Supabase deployments, apply the corresponding `postgres/migrations/005_system_logs.sql`
through the Postgres migration workflow before using persisted logs.

Request and response payloads are captured only from a small allowlist of operational
fields. Authentication, candidate, resume, interview, assessment, test, employee, AI,
and upload routes do not store bodies. Credentials, bearer tokens, and secret-like
values are redacted. SQL statements and bind parameters are never written to logs.

### 6. Azure SQL Deployment
```powershell
npm run db:azure
```

---

## Supabase Data API runtime

This is an isolated optional runtime, not part of the active SQL Server deployment. The API
uses Supabase's HTTPS Data API only when `DB_TARGET=SUPABASE` is explicitly set in the repo
`.env.local`; configure:

```dotenv
NEXT_PUBLIC_SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<server-side-service-role-key>
SUPABASE_API_TIMEOUT_S=20
```

The backend sends requests to `/rest/v1/` using the service-role key. That key bypasses row-level security, so it stays on the backend. SQL Server keeps its own `MSSQL_TARGET=local|remote|azure` selection when `DB_TARGET=MSSQL`.

In the Supabase Dashboard, open **SQL Editor**, paste `supabase/migrations/20261019130000_initial_schema.sql`, and run it. `scripts/migrate_postgres.py` can apply the files in `postgres/migrations/` when a direct Postgres connection is available.

The optional local Supabase CLI commands need Docker Desktop:

```powershell
npm run supabase:start
npm run supabase:status
npm run supabase:stop
```

---

## 🛠️ Database Troubleshooting

### 1. `Login failed for user '...'`
- **Cause**: Invalid username or password in `.env.local` or SQL Server is in Windows Authentication-only mode.
- **Fix**:
  1. Verify `MSSQL_LOCAL_USER` and `MSSQL_LOCAL_PASSWORD` in `.env.local`.
  2. In SQL Server Management Studio (SSMS), right-click your Server -> **Properties** -> **Security** -> Enable **SQL Server and Windows Authentication mode**.
  3. Under **Security** -> **Logins**, ensure the user exists, is enabled, and has `db_owner` role on the `Talentscope` database.

### 2. `TCP Provider: No connection could be made because the target machine actively refused it`
- **Cause**: SQL Server service is stopped, or TCP/IP protocol is disabled.
- **Fix**:
  1. Open **Windows Services** (`services.msc`) and verify `SQL Server (MSSQLSERVER)` is Running.
  2. Open **SQL Server Configuration Manager** -> **SQL Server Network Configuration** -> **Protocols for MSSQLSERVER**.
  3. Ensure **TCP/IP** is **Enabled**.
  4. Right-click **TCP/IP** -> **Properties** -> **IP Addresses** -> scroll to **IPAll** -> ensure **TCP Port** is `1433`. Restart the SQL Server service.

### 3. `The certificate chain was issued by an authority that is not trusted`
- **Cause**: Self-signed SSL certificate on local SQL Server.
- **Fix**: Set `MSSQL_LOCAL_TRUST_SERVER_CERTIFICATE=true` in `.env.local`.

### 4. `Migration Checksum Mismatch`
- **Cause**: An existing `.sql` migration file was modified after it was already applied to the database.
- **Fix**: Restore the original applied migration file and put the change in a new numbered migration. Keep the migration ledger and existing data intact.

---

## 📜 Adding New Migrations

1. Create a new file in the engine-specific migrations directory. For SQL Server and PostgreSQL, continue after migration `012` (for example, `013_description.sql`); use a new timestamp for Supabase.
2. Keep the script data-preserving and make DDL statements idempotent or conditional where possible.
3. If new tables are introduced, add them to `TABLES` in [`scripts/lib/tables.ts`](file:///scripts/lib/tables.ts).
4. Run `npm run db:migrate` and verify with `npm run db:status`.
