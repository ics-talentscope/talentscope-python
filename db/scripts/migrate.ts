/**
 * Apply pending T-SQL files in mssql/migrations (in filename order), each in its own transaction.
 *
 * Usage:
 *   npm run db:migrate -- --target=local
 *   npm run db:status  -- --target=remote
 */
import { createHash } from "crypto";
import { readdirSync, readFileSync } from "fs";
import { join } from "path";
import { applyTargetArg, loadProjectEnv } from "./lib/env";
import { connect, resolveMssqlTarget, sql } from "./lib/mssql";
import { MIGRATIONS_DIR } from "./lib/tables";

loadProjectEnv();
const argv = process.argv.slice(2);
applyTargetArg(argv);

type Migration = { file: string; text: string; checksum: string };

function loadMigrations(): Migration[] {
  return readdirSync(MIGRATIONS_DIR)
    .filter((name) => name.toLowerCase().endsWith(".sql"))
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    .map((file) => {
      const text = readFileSync(join(MIGRATIONS_DIR, file), "utf8").replace(/^\uFEFF/, "");
      const normalized = text.replace(/\r\n/g, "\n");
      return { file, text, checksum: createHash("sha256").update(normalized).digest("hex") };
    });
}

/** Split on GO batch separators (SQL Server tooling convention, not T-SQL). */
function splitBatches(sqlText: string): string[] {
  const batches: string[] = [];
  let current: string[] = [];
  for (const line of sqlText.split(/\r?\n/)) {
    if (/^\s*GO\s*(?:--.*)?$/i.test(line)) {
      const batch = current.join("\n").trim();
      if (batch) batches.push(batch);
      current = [];
      continue;
    }
    current.push(line);
  }
  const tail = current.join("\n").trim();
  if (tail) batches.push(tail);
  return batches;
}

function assertDataPreservingMigration(migration: Migration): void {
  const withoutComments = migration.text
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*--.*$/gm, "");
  const destructive = /\b(?:DELETE\s+FROM|DELETE\s+[A-Z_][\w$#]*\s+FROM|TRUNCATE(?:\s+TABLE)?|DROP\s+(?:TABLE|COLUMN|DATABASE|SCHEMA))\b/i;
  if (destructive.test(withoutComments)) {
    throw new Error(
      `Migration ${migration.file} contains data-destructive SQL; preserve existing rows and use additive or conditional changes.`
    );
  }
}

async function ensureMigrationsTable(pool: sql.ConnectionPool): Promise<void> {
  await pool.request().batch(`
IF OBJECT_ID(N'dbo.__migrations', N'U') IS NULL
BEGIN
  CREATE TABLE dbo.__migrations (
    id            INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    filename      NVARCHAR(260) NOT NULL UNIQUE,
    applied_at    DATETIMEOFFSET NOT NULL CONSTRAINT DF___migrations_applied_at DEFAULT (SYSUTCDATETIME())
  );
END;
IF COL_LENGTH(N'dbo.__migrations', N'checksum') IS NULL
  ALTER TABLE dbo.__migrations ADD checksum CHAR(64) NULL;
`);
}

async function appliedMap(pool: sql.ConnectionPool): Promise<Map<string, string | null>> {
  await ensureMigrationsTable(pool);
  const result = await pool
    .request()
    .query<{ filename: string; checksum: string | null }>(`SELECT filename, checksum FROM dbo.__migrations`);
  return new Map(result.recordset.map((r) => [String(r.filename), r.checksum ? String(r.checksum).trim() : null]));
}

/** Already-applied files must not change; edits would silently never reach the server. */
async function verifyApplied(pool: sql.ConnectionPool, migrations: Migration[], applied: Map<string, string | null>) {
  const edited: string[] = [];
  for (const m of migrations) {
    if (!applied.has(m.file)) continue;
    const stored = applied.get(m.file);
    if (stored == null) {
      await pool
        .request()
        .input("checksum", sql.Char(64), m.checksum)
        .input("filename", sql.NVarChar(260), m.file)
        .query(`UPDATE dbo.__migrations SET checksum = @checksum WHERE filename = @filename`);
    } else if (stored !== m.checksum) {
      edited.push(m.file);
    }
  }
  if (edited.length) {
    throw new Error(
      `These migrations were already applied but their files have been edited:\n  ${edited.join("\n  ")}\n` +
        `Revert those edits and put the change in a NEW file, e.g. mssql/migrations/NNN_description.sql.`
    );
  }
}

async function main() {
  const target = resolveMssqlTarget();
  const mode = argv.includes("status") ? "status" : "migrate";
  const migrations = loadMigrations();
  const pool = await connect(target);

  try {
    const applied = await appliedMap(pool);

    if (mode === "status") {
      for (const m of migrations) {
        const stored = applied.get(m.file);
        const state = !applied.has(m.file)
          ? "[pending]"
          : stored && stored !== m.checksum
            ? "[EDITED AFTER APPLY]"
            : "[done]";
        console.log(`  ${state} ${m.file}`);
      }
      const pending = migrations.filter((m) => !applied.has(m.file)).length;
      console.log(`${pending} pending / ${migrations.length} total`);
      return;
    }

    await verifyApplied(pool, migrations, applied);
    const pending = migrations.filter((m) => !applied.has(m.file));
    if (!pending.length) {
      console.log(`No pending migrations on ${target}.`);
      return;
    }
    pending.forEach(assertDataPreservingMigration);

    console.log(`Applying ${pending.length} migration(s) to ${target}...`);
    for (const m of pending) {
      const batches = splitBatches(m.text);
      console.log(`-> ${m.file} (${batches.length} batch${batches.length === 1 ? "" : "es"})`);
      const tx = new sql.Transaction(pool);
      await tx.begin();
      try {
        for (const batch of batches) await new sql.Request(tx).batch(batch);
        await new sql.Request(tx)
          .input("filename", sql.NVarChar(260), m.file)
          .input("checksum", sql.Char(64), m.checksum)
          .query(`INSERT INTO dbo.__migrations (filename, checksum) VALUES (@filename, @checksum)`);
        await tx.commit();
      } catch (err: any) {
        await tx.rollback().catch(() => {});
        console.error(`   FAILED ${m.file}: ${err?.message || err}`);
        throw err;
      }
    }
    console.log(`Migrations complete on ${target}.`);
  } finally {
    await pool.close();
  }
}

main().catch((err) => {
  console.error(err?.message || err);
  process.exit(1);
});
