# TalentScope Python API

FastAPI backend with SQL Server and Supabase PostgreSQL runtime targets, plus schema migration and seed-data tooling. This folder is a standalone repository root for Render or Azure VM production deployment.

## Azure production deployment

The `Python API - Deploy to Azure Production` GitHub Actions workflow deploys pushes to `main` and can also be run manually with production confirmation. The development workflow deploys pushes to `develop`. Validation runs before deployment, which uses the `self-hosted`, `linux`, and `icsbetintapp01` runner labels.

The runner syncs this repository to `/opt/talentscope/talentscope-UI/python` and updates the existing `python` Compose service using `/opt/talentscope/talentscope-UI/docker-compose.azure.yml`. Production settings remain on the VM in `/opt/talentscope/talentscope-UI/.env.azure`; do not add them to GitHub or this repository.

The Employee Portal has separate Excel exports for detailed results and a summary workbook. The summary includes overall totals plus domain, product, and domain-by-product breakdowns for all portal records.

## Deploy on Render

Create a Render Blueprint from this repository and use `render.yaml`, or create a Docker web service with this folder as the build context and `Dockerfile` as the Dockerfile. The API listens on port `8000`; Render checks `/api/health`.

The API requires an existing, network-accessible database. Select it with `DB_TARGET=MSSQL` or `DB_TARGET=SUPABASE`. For SQL Server, set `MSSQL_TARGET` and its matching settings; Render's blueprint defaults to Azure SQL. For Supabase, configure the project URL and a server-only service-role key for the HTTPS Data API; a PostgreSQL password is not required. Configure `FRONTEND_ORIGIN` with the Vercel production origin, and set the auth passwords and secrets before opening the app to users. Set `FACE_MATCH_SERVICE_URL` and `FACE_MATCH_API_KEY` to the separately deployed Face Match service values. `LOCAL_LLM_URL` is optional; the API uses its fallback behavior when it is unavailable.

The blueprint attaches a persistent disk at `/app/uploads` for uploaded documents and recordings. Keep that disk attached when redeploying.

## Application logs

Local generated logs under `logs/` are ignored by Git and excluded from Docker builds
and Azure deployment syncs. They are not committed or copied from a developer machine.
On the Azure VM, the running API writes its logs to the separately mounted `/opt/logs`
directory; deployment updates application code without replacing or syncing those VM
log files. See [`db/README.md`](./db/README.md#application-logs) for the daily files
and monthly archive layout.

### Azure VM uploads and recordings

The VM deployment workflows mount `/opt/recordings/prod` or `/opt/recordings/dev` at `/app/uploads`; production and development therefore keep separate persistent data outside the application checkout and Docker image. Employee-test videos remain in the `employee_test_recordings/` subfolder and retain their `<test-id>.webm` names, which the API uses to associate each video with its database test/employee record.

Before recreating the API container, deployment copies the existing `/app/uploads` contents from its current Docker volume into the matching `/opt/recordings/<environment>` directory. This is a copy, not a destructive move: the old volume is retained as a recovery copy, and subsequent builds/redeployments do not sync or delete files from `/opt/recordings`. Verify the recordings in the application before manually removing any old Docker volume.

Development Employee Portal exports use the production API as their source so the detailed Excel export and summary use the same employee data and mapping as production. Set `PORTAL_EXPORT_API_URL` in the development environment file to the production API base URL (HTTPS, without a trailing path to the export endpoint). The development deployment requires this setting. The dev API forwards the signed-in admin's bearer token, and production performs its normal authentication and permission checks; the admin token signing secret and account permissions must therefore be valid in both environments. If the production API rejects the token or is unavailable, the export fails instead of falling back to development data. Production exports continue to use their local production data.

## Database migrations and seeds

The `db/` directory contains SQL migrations, seed data, and the Node-based database CLI. Run migrations deliberately from an administrator/developer machine that can reach the target database; deployment does not automatically seed production data.

```powershell
Copy-Item .env.example .env
# Edit .env with the database target and credentials.
cd db
npm ci
npm run db:status
npm run db:migrate
```

Seed JSON files are excluded from Git and Docker because they contain personal data. Supply approved, sanitized seed files privately before running `npm run db:deploy-data` or `npm run db:azure`. Schema-only Azure migrations use `npm run db:migrate -- --target=azure`.

## Employee Portal user management

Admins with **Portal Config** access can open **Portal Config → Employee Portal Users** to upload product question banks, provision one employee, or bulk-import a roster. The roster accepts `.xlsx`, `.xlsm`, or UTF-8 `.csv` files with `Emp ID`, `Name`, `Email` (including `Nokia Email ID`), and `Product` columns; `Role`/`Designation` is optional. Product names must match a configured product bank. Admins with **Employee Portal** access can edit managed users inline and reset their passwords from the Actions column in the main Employee Portal table.

Question banks support the stacked product-sheet format and wide MCQ format used by the schedule samples. Select the product sheet when needed and set the number of questions assigned per employee. Each new assessment samples that number of questions randomly from the uploaded bank for that product. Re-imports update employee details without resetting a matching existing assessment or password.

New users sign in with their Employee ID as the initial password. Changing it after signing in is recommended, but not forced. Password reset and user-initiated password changes require a one-time email code. Admins can reset a portal user's password to their Employee ID from the Employee Portal table's Actions column and email the temporary credentials to the registered address. After a reset, the employee can sign in without a forced password-change prompt; the email recommends changing the temporary password. Configure working SMTP delivery before enabling these flows; simulated or failed mail delivery is reported and never presented as a delivered email.

The employee roster shows a green **Verified** badge after the employee successfully verifies their current email with an OTP during password reset or change. Updating the email address clears the badge until the new address is verified.

Apply `db/mssql/migrations/010_employee_portal_management.sql` and `011_employee_email_verified.sql` before using employee provisioning with SQL Server. PostgreSQL deployments must apply both corresponding migrations in `db/postgres/migrations/`; Supabase deployments must apply `db/supabase/migrations/20261027210000_employee_portal_management.sql` and `20261027231500_employee_email_verified.sql`.

### Optional Supabase local database

The Supabase setup under `db/supabase/` contains the PostgreSQL schema. The API connects to a hosted Supabase project through the HTTPS Data API; no Supabase Docker stack or database password is needed.

```powershell
Copy-Item .env.example .env.local
# Set DB_TARGET=SUPABASE and fill NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.
# Keep the service-role key server-side only; rotate any key that was shared or exposed.
uvicorn main:app --port 8000 --reload
```

Apply the PostgreSQL migration in `db/supabase/migrations/` through the Supabase Dashboard SQL Editor before switching the API. See [`db/README.md`](./db/README.md#supabase-data-api-runtime) for details.

## Local development

Local uploads and recordings are stored under `recordings/` by default. The folder
is present in the repository, but its runtime contents are ignored by Git.

Install `requirements.txt`, install Microsoft's ODBC Driver 18 for SQL Server, and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --port 8000 --reload
```

For UI/API endpoint integration checks, keep the UI checkout beside this repository or set `TALENTSCOPE_UI_DIR` to its path, then run `python scripts/check_api_links.py`.
