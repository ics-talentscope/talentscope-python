# TalentScope Python API

FastAPI backend currently deployed to Azure VMs with SQL Server, plus schema migration and seed-data tooling. Supabase and third-party hosting assets are retained as isolated optional integrations.

## Azure production deployment

The `Python API - Deploy to Azure Production` GitHub Actions workflow deploys pushes to `main` and can also be run manually with production confirmation. The development workflow deploys pushes to `develop`. Validation runs before deployment, which uses the `self-hosted`, `linux`, and `icsbetintapp01` runner labels.

The development and production runners sync this repository, including `db/` migration files, to `/opt/talentscope-dev/python` and `/opt/talentscope/python`. They verify every synchronized SQL migration before deployment. They combine the VM's shared `docker-compose.azure.yml` with this repository's `compose.api.yml` and `compose.recordings.yml`, so API deployment does not depend on the shared Compose file already defining a `python` service. Runtime environment files remain on the VM at `/opt/talentscope-dev/.env.local` and `/opt/talentscope/.env.local`; do not add them to GitHub or this repository.

The Employee Portal has separate Excel exports for detailed results and a summary workbook. The summary includes overall totals plus domain, product, and domain-by-product breakdowns for all portal records.

## Isolated optional providers

The active deployment path is the Azure VM workflow above with SQL Server. Render and Vercel are not currently used; their manifests and adapter are kept outside the repository root so hosting platforms do not discover them automatically:

- API Render blueprint: [`deploy/optional/render/render.yaml`](./deploy/optional/render/render.yaml)
- Vercel manifest and Python adapter: [`deploy/optional/vercel/`](./deploy/optional/vercel/)
- Face Match Render blueprint: [`../python-ai/deploy/optional/render/render.yaml`](../python-ai/deploy/optional/render/render.yaml)

To re-enable a Render blueprint, copy its manifest to the repository root before connecting the repository. To re-enable Vercel, copy `deploy/optional/vercel/vercel.json` to the repository root and keep its referenced adapter at `deploy/optional/vercel/api/index.py`.

The API defaults to SQL Server. Supabase remains an optional database adapter and is selected only by explicitly setting `DB_TARGET=SUPABASE`; the `db/postgres/` and `db/supabase/` schemas and migration tooling are not part of the active SQL Server migration path. Do not set that target in the active SQL Server deployment.

## Application logs

Local generated logs under `logs/` are ignored by Git and excluded from Docker builds
and Azure deployment syncs. They are not committed or copied from a developer machine.
On the Azure VM, dev logs are written to `/opt/logs/dev` and production logs to
`/opt/logs/prod`. These host directories are mounted separately; deployment updates
application code without syncing or replacing VM log files. See
[`db/README.md`](./db/README.md#application-logs) for the daily files and monthly
archive layout.

### Azure VM uploads and recordings

The VM deployment workflows mount `/opt/recordings/dev` for development and `/opt/recordings/prod` for production at `/app/uploads`; production and development therefore keep separate persistent data outside the application checkout and Docker image. Files are stored flat in that directory with descriptive prefixes (`interview-recording-<id>.webm`, `test-recording-<test-id>.webm`, `verification-<id>_id.jpg`, `resume-<id>-<name>`, `backup-<test-id>-latest.json`/`-submit.json`, `doc-<category>-<name>`); other clients are prefixed with their name. To convert an existing nested directory run `python scripts/migrate_storage_layout.py --recordings <dir> --logs <logs-dir>` (dry run) and add `--apply`.

Before recreating the API container, deployment copies the existing `/app/uploads` contents from its current Docker volume into the matching environment directory. This is a copy, not a destructive move: the old volume is retained as a recovery copy, and subsequent builds/redeployments do not sync or delete files from either recordings directory. Verify the recordings in the application before manually removing any old Docker volume.

The VM host ports remain `8001` for the development API and `8000` for production;
both containers listen on port `8000` internally. The development and production UI
ports remain `3001` and `3000`, respectively. The shared Python AI service remains on
host port `10000`.

Development Employee Portal exports use the production API as their source so the detailed Excel export and summary use the same employee data and mapping as production. Set `PORTAL_EXPORT_API_URL` in the development environment file to the production API base URL (HTTPS, without a trailing path to the export endpoint). The dev deployment can proceed without this setting, but its portal exports return an explicit configuration error until it is set; they never silently fall back to development data. The dev API forwards the signed-in admin's bearer token, and production performs its normal authentication and permission checks; the admin token signing secret and account permissions must therefore be valid in both environments. If the production API rejects the token or is unavailable, the export fails. Production exports continue to use their local production data.

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

Admins with **Portal Config** access can open **Portal Config → Employee Portal Users** to upload product question banks, provision one employee, or bulk-import a roster. AI Questionnaire is the default question source for both single-user and bulk provisioning; Product is optional in AI mode. For bank mode, select **Product Question Bank** and provide a `Product` or `Product Name` column whose values match configured banks; each employee is assigned questions from that product's bank. The roster accepts `.xlsx`, `.xlsm`, or UTF-8 `.csv` files with `Emp ID`, `Name`, and `Email` (including `Nokia Email ID`); `Role`/`Designation` and `Product` are optional for AI mode. AI employees can choose learning topics grouped by their Basic, Intermediate, or High difficulty, and each selected quiz generates multiple-choice questions with answer keys. If the local LLM is unavailable, the built-in topic question fallback keeps the quiz available and logs the generation failure. Admins with **Employee Portal** access can edit managed users inline, reset passwords from the Actions column, and permanently delete selected portal accounts together with their assessments, answers, and stored videos from the main Employee Portal table.

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
