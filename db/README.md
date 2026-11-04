# TalentScope Database (MS SQL Server)

This directory contains the database schema migrations, optional baseline datasets, and deployment scripts for **Microsoft SQL Server** (local, remote, or Azure SQL), plus the **Supabase/PostgreSQL** schema. The Python API can connect to either database using `DB_TARGET`. The seed JSON files are excluded from Git and Docker by default because they contain personal data; provide approved datasets privately if you need to seed a database.

---

## 📁 Directory Structure

| Path | Description |
|---|---|
| `mssql/migrations/*.sql` | Ordered SQL DDL migrations (applied sequentially, tracked in `__migrations` table) |
| `mssql/data/*.json` | Baseline table seed datasets (employees, job descriptions, test questions, settings) |
| `scripts/migrate.ts` | Migration runner (applies unapplied `.sql` scripts and verifies checksums) |
| `scripts/deploy-data.ts` | Data deployment engine (inserts or upserts baseline datasets into target DB) |
| `scripts/bootstrap.ts` | Creates the database instance if it does not yet exist |
| `scripts/lib/mssql.ts` | Database connection pool configuration and target resolution |
| `mssql/migrations/005_system_logs.sql` | Structured application/request/database log storage |
| `supabase/config.toml` | Supabase CLI local-development configuration |
| `supabase/migrations/*.sql` | PostgreSQL schema migrations for Supabase |
| `postgres/migrations/*.sql` | PostgreSQL migrations applied by `scripts/migrate_postgres.py` |

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

### 5. Combined One-Step Setup (Local)
Runs both migrations and baseline data deployment against local SQL Server:
```powershell
npm run db:local
```

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

The API uses Supabase's HTTPS Data API. Set `DB_TARGET=SUPABASE` in the repo `.env.local`, and configure:

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
- **Fix**: Never edit applied migrations. Create a new migration file (e.g. `006_new_feature.sql`). If developing locally and resetting:
  ```sql
  DROP TABLE __migrations;
  ```
  Then re-run `npm run db:migrate`.

---

## 📜 Adding New Migrations

1. Create a new file in `mssql/migrations/` following the naming convention `00X_description.sql`.
2. Ensure DDL statements are idempotent or safe for migration.
3. If new tables are introduced, add them to `TABLES` in [`scripts/lib/tables.ts`](file:///scripts/lib/tables.ts).
4. Run `npm run db:migrate` and verify with `npm run db:status`.
