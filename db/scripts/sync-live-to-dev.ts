/**
 * One-way copy: rows saved in TalentScope_DB_Live -> the database the VM app uses (MSSQL_DATABASE).
 * Reads Live only; every write goes to the connected (app) database. Never writes Live.
 *
 * A Live row is copied when it is new or its contents changed since it was last copied (SHA-256 of the row,
 * kept in dbo.__live_sync_rows). Employee ID is the stable identity: when the same employee exists in both
 * databases under different GUIDs, Dev is remapped to Live's keys (employees, then tests, questions, and
 * sessions) and Live's columns overwrite Dev. Unique-key clashes therefore take Live, not the app copy.
 * Product-bank settings are merged with Live taking precedence for shared products; Dev-only products are retained.
 *
 * Usage: tsx scripts/sync-live-to-dev.ts [--tables=a,b] [--dry-run] [--interval-minutes=N]
 * Env: MSSQL_* (app database, the target), SYNC_SOURCE_DATABASE (default TalentScope_DB_Live),
 *      SYNC_INTERVAL_MINUTES (optional loop when --interval-minutes is omitted).
 */
import { applyTargetArg, argValue, loadProjectEnv } from "./lib/env";
import { connect, resolveMssqlTarget, sql } from "./lib/mssql";
import { quoteIdent, selectTables, type TableDef } from "./lib/tables";

loadProjectEnv();
const argv = process.argv.slice(2);
applyTargetArg(argv);

const SOURCE = process.env.SYNC_SOURCE_DATABASE || "TalentScope_DB_Live";
const DRY_RUN = argv.includes("--dry-run");
/** Owned by the VM app (Corp Pool roster, admin settings). */
const EXCLUDED = new Set(["portal_settings"]);
const PRODUCT_BANK_SETTING = "employee_portal_products";
const SKIP_TYPES = ["timestamp", "xml", "text", "ntext", "image", "sql_variant", "geography", "geometry", "hierarchyid"];

type Pool = sql.ConnectionPool;
type Changed = { k: string; h: Buffer };
type Result = {
  changed: number;
  inserted: number;
  updated: number;
  conflicts: number;
  conflictDetails?: string[];
  seeded?: number;
  notInApp?: number;
};
type IdentityAdopt = {
  employees: number;
  tests: number;
  questions: number;
  sessions: number;
};

type Shape = { cols: string[]; identity: boolean; textual: Set<string> };

function intervalMinutes(): number {
  const raw = argValue(argv, "interval-minutes") ?? process.env.SYNC_INTERVAL_MINUTES ?? "0";
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

async function columnsFor(pool: Pool, table: string): Promise<Shape | null> {
  const result = await pool.request().input("src", sql.NVarChar, `${SOURCE}.dbo.${table}`).input("dst", sql.NVarChar, `dbo.${table}`).query(`
    SELECT s.name, CAST(d.is_identity AS int) AS isIdentity,
           CASE WHEN s.collation_name IS NOT NULL OR d.collation_name IS NOT NULL THEN 1 ELSE 0 END AS textual
    FROM [${SOURCE}].sys.columns s
    JOIN sys.columns d ON d.object_id = OBJECT_ID(@dst) AND d.name = s.name
    WHERE s.object_id = OBJECT_ID(@src) AND s.is_computed = 0 AND d.is_computed = 0
      AND TYPE_NAME(s.system_type_id) NOT IN (${SKIP_TYPES.map((t) => `'${t}'`).join(", ")})
      AND TYPE_NAME(d.system_type_id) NOT IN (${SKIP_TYPES.map((t) => `'${t}'`).join(", ")})
    ORDER BY s.column_id`);
  if (!result.recordset.length) return null;
  return {
    cols: result.recordset.map((r) => String(r.name)),
    identity: result.recordset.some((r) => r.isIdentity),
    textual: new Set(result.recordset.filter((r) => r.textual).map((r) => String(r.name))),
  };
}

function statements(table: TableDef, shape: Shape) {
  const pk = table.pk[0];
  const src = `[${SOURCE}].dbo.${quoteIdent(table.name)}`;
  const dst = `dbo.${quoteIdent(table.name)}`;
  const q = shape.cols.map(quoteIdent);
  // Live and the app database may use different collations; compare text in the app database's collation.
  const srcCol = (c: string) => `s.${quoteIdent(c)}${shape.textual.has(c) ? " COLLATE DATABASE_DEFAULT" : ""}`;
  const nonKey = shape.cols.filter((c) => c !== pk);
  const key = (alias: string) => `CONVERT(nvarchar(200), ${alias}.${quoteIdent(pk)}) COLLATE DATABASE_DEFAULT`;
  const hash = `HASHBYTES('SHA2_256', (SELECT ${q.map((c) => `s.${c}`).join(", ")} FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES))`;
  const join = `d.${quoteIdent(pk)} = ${srcCol(pk)}`;
  const sourceScope = table.name === "portal_settings" ? `s.${quoteIdent(pk)} = N'${PRODUCT_BANK_SETTING}'` : "1=1";
  const update = nonKey.length
    ? `UPDATE d SET ${nonKey.map((c) => `d.${quoteIdent(c)} = s.${quoteIdent(c)}`).join(", ")}
       FROM ${dst} d JOIN ${src} s ON ${join} JOIN #chg c ON c.k = ${key("s")}
       WHERE ${sourceScope} AND EXISTS (SELECT ${nonKey.map(srcCol).join(", ")} EXCEPT SELECT ${nonKey.map((c) => `d.${quoteIdent(c)}`).join(", ")});`
    : "";
  return {
    hashes: `SELECT ${key("s")} AS k, ${hash} AS h FROM ${src} s WHERE ${sourceScope}`,
    missingInApp: `SELECT COUNT(*) AS n FROM ${src} s WHERE ${sourceScope} AND NOT EXISTS (SELECT 1 FROM ${dst} d WHERE ${join})`,
    apply: `
      DECLARE @updated int = 0, @inserted int = 0;
      ${update}
      SET @updated = @@ROWCOUNT;
      INSERT INTO ${dst} (${q.join(", ")})
      SELECT ${q.map((c) => `s.${c}`).join(", ")} FROM ${src} s JOIN #chg c ON c.k = ${key("s")}
      WHERE ${sourceScope} AND NOT EXISTS (SELECT 1 FROM ${dst} d WHERE ${join});
      SET @inserted = @@ROWCOUNT;
      SELECT @updated AS updated, @inserted AS inserted;`,
  };
}

async function loadChg(tx: sql.Transaction, rows: Changed[]) {
  await new sql.Request(tx).batch(
    "IF OBJECT_ID('tempdb..#chg') IS NOT NULL DROP TABLE #chg; CREATE TABLE #chg (k nvarchar(200) COLLATE DATABASE_DEFAULT NOT NULL PRIMARY KEY, h varbinary(32) NOT NULL);"
  );
  const t = new sql.Table("#chg");
  t.create = false;
  t.columns.add("k", sql.NVarChar(200), { nullable: false });
  t.columns.add("h", sql.VarBinary(32), { nullable: false });
  for (const r of rows) t.rows.add(r.k, r.h);
  await new sql.Request(tx).bulk(t);
}

async function recordState(tx: sql.Transaction, table: string) {
  await new sql.Request(tx).input("t", sql.NVarChar(64), table).query(`
    MERGE dbo.__live_sync_rows AS r USING #chg AS c ON r.table_name = @t AND r.row_key = c.k
    WHEN MATCHED THEN UPDATE SET row_hash = c.h, synced_at = SYSUTCDATETIME()
    WHEN NOT MATCHED THEN INSERT (table_name, row_key, row_hash) VALUES (@t, c.k, c.h);`);
}

/** Apply `rows` in one transaction; returns counts, or throws (rolled back) if any row is rejected. */
async function applyBatch(pool: Pool, table: TableDef, apply: string, rows: Changed[]) {
  const tx = new sql.Transaction(pool);
  await tx.begin();
  try {
    await loadChg(tx, rows);
    const r = (await new sql.Request(tx).query(apply)).recordset[0] ?? {};
    await recordState(tx, table.name);
    if (DRY_RUN) await tx.rollback();
    else await tx.commit();
    return { updated: Number(r.updated || 0), inserted: Number(r.inserted || 0) };
  } catch (err) {
    await tx.rollback().catch(() => {});
    throw err;
  }
}

/**
 * Point Dev at Live's GUIDs when the business key already matches.
 * Employee ID is the permanent relation; tests follow (employee + topic), questions (test + index),
 * and candidate sessions (email or code).
 */
async function adoptLiveIdentities(pool: Pool): Promise<IdentityAdopt> {
  const tx = new sql.Transaction(pool);
  await tx.begin();
  try {
    const counts = (
      await new sql.Request(tx).query(`
        SET NOCOUNT ON;
        IF OBJECT_ID('tempdb..#emp_map') IS NOT NULL DROP TABLE #emp_map;
        IF OBJECT_ID('tempdb..#test_map') IS NOT NULL DROP TABLE #test_map;
        IF OBJECT_ID('tempdb..#q_map') IS NOT NULL DROP TABLE #q_map;
        IF OBJECT_ID('tempdb..#sess_map') IS NOT NULL DROP TABLE #sess_map;
        CREATE TABLE #emp_map (live_id uniqueidentifier NOT NULL PRIMARY KEY, dev_id uniqueidentifier NOT NULL UNIQUE);
        CREATE TABLE #test_map (live_id uniqueidentifier NOT NULL PRIMARY KEY, dev_id uniqueidentifier NOT NULL UNIQUE);
        CREATE TABLE #q_map (live_id uniqueidentifier NOT NULL PRIMARY KEY, dev_id uniqueidentifier NOT NULL UNIQUE);
        CREATE TABLE #sess_map (live_id uniqueidentifier NOT NULL PRIMARY KEY, dev_id uniqueidentifier NOT NULL UNIQUE);

        INSERT INTO #emp_map (live_id, dev_id)
        SELECT live_id, dev_id FROM (
          SELECT s.id AS live_id, d.id AS dev_id,
                 ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY d.id) AS rn_live,
                 ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY s.id) AS rn_dev
          FROM [${SOURCE}].dbo.employees s
          JOIN dbo.employees d
            ON s.employee_id COLLATE DATABASE_DEFAULT = d.employee_id COLLATE DATABASE_DEFAULT
           AND s.id <> d.id
          WHERE NOT EXISTS (SELECT 1 FROM dbo.employees x WHERE x.id = s.id)
        ) m WHERE rn_live = 1 AND rn_dev = 1;

        INSERT INTO #test_map (live_id, dev_id)
        SELECT live_id, dev_id FROM (
          SELECT s.id AS live_id, d.id AS dev_id,
                 ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY d.id) AS rn_live,
                 ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY s.id) AS rn_dev
          FROM [${SOURCE}].dbo.tests s
          JOIN [${SOURCE}].dbo.employees se ON se.id = s.employee_id
          JOIN dbo.employees de
            ON de.employee_id COLLATE DATABASE_DEFAULT = se.employee_id COLLATE DATABASE_DEFAULT
          JOIN dbo.tests d
            ON d.employee_id = de.id
           AND d.topic_id COLLATE DATABASE_DEFAULT = s.topic_id COLLATE DATABASE_DEFAULT
           AND d.id <> s.id
          WHERE NOT EXISTS (SELECT 1 FROM dbo.tests x WHERE x.id = s.id)
        ) m WHERE rn_live = 1 AND rn_dev = 1;

        INSERT INTO #q_map (live_id, dev_id)
        SELECT live_id, dev_id FROM (
          SELECT s.id AS live_id, d.id AS dev_id,
                 ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY d.id) AS rn_live,
                 ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY s.id) AS rn_dev
          FROM [${SOURCE}].dbo.test_questions s
          JOIN dbo.test_questions d
            ON d.question_index = s.question_index
           AND d.id <> s.id
           AND (
                d.test_id = s.test_id
             OR EXISTS (SELECT 1 FROM #test_map t WHERE t.live_id = s.test_id AND t.dev_id = d.test_id)
           )
          WHERE NOT EXISTS (SELECT 1 FROM dbo.test_questions x WHERE x.id = s.id)
        ) m WHERE rn_live = 1 AND rn_dev = 1;

        INSERT INTO #sess_map (live_id, dev_id)
        SELECT live_id, dev_id FROM (
          SELECT s.id AS live_id, d.id AS dev_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY s.id
                   ORDER BY CASE
                     WHEN s.email IS NOT NULL AND d.email COLLATE DATABASE_DEFAULT = s.email COLLATE DATABASE_DEFAULT THEN 0
                     ELSE 1
                   END, d.id
                 ) AS rn_live,
                 ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY s.id) AS rn_dev
          FROM [${SOURCE}].dbo.candidate_sessions s
          JOIN dbo.candidate_sessions d ON d.id <> s.id
           AND (
                (s.email IS NOT NULL AND d.email COLLATE DATABASE_DEFAULT = s.email COLLATE DATABASE_DEFAULT)
             OR (s.code IS NOT NULL AND d.code COLLATE DATABASE_DEFAULT = s.code COLLATE DATABASE_DEFAULT)
           )
          WHERE NOT EXISTS (SELECT 1 FROM dbo.candidate_sessions x WHERE x.id = s.id)
        ) m WHERE rn_live = 1 AND rn_dev = 1;

        DECLARE @remap_fks TABLE (
          schema_name sysname NOT NULL,
          table_name sysname NOT NULL,
          constraint_name sysname NOT NULL,
          PRIMARY KEY (schema_name, table_name, constraint_name)
        );
        INSERT INTO @remap_fks (schema_name, table_name, constraint_name)
        SELECT ps.name, pt.name, fk.name
        FROM sys.foreign_keys fk
        JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
        JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
        JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
        JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        WHERE ps.name = N'dbo' AND rs.name = N'dbo'
          AND (
            (pt.name = N'tests' AND rt.name = N'employees')
            OR (pt.name = N'test_questions' AND rt.name = N'tests')
            OR (pt.name = N'test_attempts' AND rt.name IN (N'employees', N'tests', N'test_questions'))
            OR (pt.name = N'employee_auth_otps' AND rt.name = N'employees')
          );

        DECLARE @fk_schema sysname, @fk_table sysname, @fk_name sysname, @fk_sql nvarchar(1000);
        DECLARE disable_remap_fks CURSOR LOCAL FAST_FORWARD FOR
          SELECT schema_name, table_name, constraint_name FROM @remap_fks;
        OPEN disable_remap_fks;
        FETCH NEXT FROM disable_remap_fks INTO @fk_schema, @fk_table, @fk_name;
        WHILE @@FETCH_STATUS = 0
        BEGIN
          SET @fk_sql = N'ALTER TABLE ' + QUOTENAME(@fk_schema) + N'.' + QUOTENAME(@fk_table)
            + N' NOCHECK CONSTRAINT ' + QUOTENAME(@fk_name) + N';';
          EXEC sys.sp_executesql @fk_sql;
          FETCH NEXT FROM disable_remap_fks INTO @fk_schema, @fk_table, @fk_name;
        END;
        CLOSE disable_remap_fks;
        DEALLOCATE disable_remap_fks;

        UPDATE t SET t.employee_id = m.live_id
        FROM dbo.tests t JOIN #emp_map m ON t.employee_id = m.dev_id;
        UPDATE t SET t.employee_id = m.live_id
        FROM dbo.test_attempts t JOIN #emp_map m ON t.employee_id = m.dev_id;
        IF OBJECT_ID(N'dbo.employee_auth_otps', N'U') IS NOT NULL
          UPDATE t SET t.employee_id = m.live_id
          FROM dbo.employee_auth_otps t JOIN #emp_map m ON t.employee_id = m.dev_id;
        UPDATE e SET e.id = m.live_id
        FROM dbo.employees e JOIN #emp_map m ON e.id = m.dev_id;

        UPDATE q SET q.test_id = m.live_id
        FROM dbo.test_questions q JOIN #test_map m ON q.test_id = m.dev_id;
        UPDATE a SET a.test_id = m.live_id
        FROM dbo.test_attempts a JOIN #test_map m ON a.test_id = m.dev_id;
        UPDATE t SET t.id = m.live_id
        FROM dbo.tests t JOIN #test_map m ON t.id = m.dev_id;

        UPDATE a SET a.question_id = m.live_id
        FROM dbo.test_attempts a JOIN #q_map m ON a.question_id = m.dev_id;
        UPDATE q SET q.id = m.live_id
        FROM dbo.test_questions q JOIN #q_map m ON q.id = m.dev_id;

        UPDATE s SET s.id = m.live_id
        FROM dbo.candidate_sessions s JOIN #sess_map m ON s.id = m.dev_id;

        DECLARE enable_remap_fks CURSOR LOCAL FAST_FORWARD FOR
          SELECT schema_name, table_name, constraint_name FROM @remap_fks;
        OPEN enable_remap_fks;
        FETCH NEXT FROM enable_remap_fks INTO @fk_schema, @fk_table, @fk_name;
        WHILE @@FETCH_STATUS = 0
        BEGIN
          SET @fk_sql = N'ALTER TABLE ' + QUOTENAME(@fk_schema) + N'.' + QUOTENAME(@fk_table)
            + N' WITH CHECK CHECK CONSTRAINT ' + QUOTENAME(@fk_name) + N';';
          EXEC sys.sp_executesql @fk_sql;
          FETCH NEXT FROM enable_remap_fks INTO @fk_schema, @fk_table, @fk_name;
        END;
        CLOSE enable_remap_fks;
        DEALLOCATE enable_remap_fks;

        DELETE r FROM dbo.__live_sync_rows r
        JOIN #emp_map m ON r.table_name = N'employees'
         AND r.row_key = CONVERT(nvarchar(200), m.live_id) COLLATE DATABASE_DEFAULT;
        DELETE r FROM dbo.__live_sync_rows r
        JOIN #test_map m ON r.table_name = N'tests'
         AND r.row_key = CONVERT(nvarchar(200), m.live_id) COLLATE DATABASE_DEFAULT;
        DELETE r FROM dbo.__live_sync_rows r
        JOIN #q_map m ON r.table_name = N'test_questions'
         AND r.row_key = CONVERT(nvarchar(200), m.live_id) COLLATE DATABASE_DEFAULT;
        DELETE r FROM dbo.__live_sync_rows r
        JOIN #sess_map m ON r.table_name = N'candidate_sessions'
         AND r.row_key = CONVERT(nvarchar(200), m.live_id) COLLATE DATABASE_DEFAULT;

        DELETE r FROM dbo.__live_sync_rows r
        WHERE r.table_name = N'employees'
          AND NOT EXISTS (
            SELECT 1 FROM dbo.employees d
            WHERE CONVERT(nvarchar(200), d.id) COLLATE DATABASE_DEFAULT = r.row_key
          );
        DELETE r FROM dbo.__live_sync_rows r
        WHERE r.table_name = N'tests'
          AND NOT EXISTS (
            SELECT 1 FROM dbo.tests d
            WHERE CONVERT(nvarchar(200), d.id) COLLATE DATABASE_DEFAULT = r.row_key
          );
        DELETE r FROM dbo.__live_sync_rows r
        WHERE r.table_name = N'test_questions'
          AND NOT EXISTS (
            SELECT 1 FROM dbo.test_questions d
            WHERE CONVERT(nvarchar(200), d.id) COLLATE DATABASE_DEFAULT = r.row_key
          );
        DELETE r FROM dbo.__live_sync_rows r
        WHERE r.table_name = N'test_attempts'
          AND NOT EXISTS (
            SELECT 1 FROM dbo.test_attempts d
            WHERE CONVERT(nvarchar(200), d.id) COLLATE DATABASE_DEFAULT = r.row_key
          );
        DELETE r FROM dbo.__live_sync_rows r
        WHERE r.table_name = N'candidate_sessions'
          AND NOT EXISTS (
            SELECT 1 FROM dbo.candidate_sessions d
            WHERE CONVERT(nvarchar(200), d.id) COLLATE DATABASE_DEFAULT = r.row_key
          );

        SELECT
          (SELECT COUNT(*) FROM #emp_map) AS employees,
          (SELECT COUNT(*) FROM #test_map) AS tests,
          (SELECT COUNT(*) FROM #q_map) AS questions,
          (SELECT COUNT(*) FROM #sess_map) AS sessions;
      `)
    ).recordset[0] ?? {};
    if (DRY_RUN) await tx.rollback();
    else await tx.commit();
    return {
      employees: Number(counts.employees || 0),
      tests: Number(counts.tests || 0),
      questions: Number(counts.questions || 0),
      sessions: Number(counts.sessions || 0),
    };
  } catch (err) {
    await tx.rollback().catch(() => {});
    throw err;
  }
}

/** Rows that still cannot land because a parent Live row is missing in Dev. Unique-key clashes are remapped, not skipped. */
async function conflictingRows(pool: Pool, table: TableDef, changed: Changed[]) {
  const conflictQueries: Record<string, string> = {
    candidate_sessions: `
      SELECT CONVERT(nvarchar(200), s.id) AS k, CAST(NULL AS nvarchar(200)) AS detail
      FROM [${SOURCE}].dbo.candidate_sessions s
      WHERE s.resume_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dbo.resumes r WHERE r.id = s.resume_id)`,
    tests: `
      SELECT CONVERT(nvarchar(200), s.id) AS k, CAST(NULL AS nvarchar(200)) AS detail
      FROM [${SOURCE}].dbo.tests s
      WHERE NOT EXISTS (SELECT 1 FROM dbo.employees e WHERE e.id = s.employee_id)`,
    test_questions: `
      SELECT CONVERT(nvarchar(200), s.id) AS k, CAST(NULL AS nvarchar(200)) AS detail
      FROM [${SOURCE}].dbo.test_questions s
      WHERE NOT EXISTS (SELECT 1 FROM dbo.tests t WHERE t.id = s.test_id)`,
    test_attempts: `
      SELECT CONVERT(nvarchar(200), s.id) AS k, CAST(NULL AS nvarchar(200)) AS detail
      FROM [${SOURCE}].dbo.test_attempts s
      WHERE NOT EXISTS (SELECT 1 FROM dbo.tests t WHERE t.id = s.test_id)
         OR NOT EXISTS (SELECT 1 FROM dbo.test_questions q WHERE q.id = s.question_id)
         OR NOT EXISTS (SELECT 1 FROM dbo.employees e WHERE e.id = s.employee_id)`,
  };
  const query = conflictQueries[table.name];
  if (!query) return { rows: [] as Changed[], details: [] as string[] };
  const rows = (await pool.request().query(query)).recordset;
  const changedKeys = new Set(changed.map((row) => row.k.toLowerCase()));
  const byKey = new Map(changed.map((row) => [row.k.toLowerCase(), row]));
  const conflicts = rows.filter((row) => changedKeys.has(String(row.k).toLowerCase()));
  return {
    rows: conflicts.map((row) => byKey.get(String(row.k).toLowerCase())!).filter(Boolean),
    details: conflicts.flatMap((row) => (row.detail == null ? [] : [String(row.detail)])),
  };
}

async function syncTable(pool: Pool, table: TableDef): Promise<Result | string> {
  if (table.pk.length !== 1) return "composite key, skipped";
  const shape = await columnsFor(pool, table.name);
  if (!shape) return "not in both databases, skipped";
  if (!shape.cols.includes(table.pk[0])) return `key ${table.pk[0]} missing, skipped`;
  if (shape.identity) return "identity column, skipped";
  const st = statements(table, shape);

  const seeded = (
    await pool.request().input("t", sql.NVarChar(64), table.name).query("SELECT TOP 1 1 AS x FROM dbo.__live_sync_rows WHERE table_name = @t")
  ).recordset.length > 0;

  const changed = (
    await pool.request().input("t", sql.NVarChar(64), table.name).query(`
      SELECT x.k, x.h FROM (${st.hashes}) x
      WHERE NOT EXISTS (SELECT 1 FROM dbo.__live_sync_rows r WHERE r.table_name = @t AND r.row_key = x.k AND r.row_hash = x.h)`)
  ).recordset as Changed[];
  const changedCount = changed.length;
  let conflictCount = 0;
  let conflictDetails: string[] = [];
  let applicable = changed;
  if (changed.length) {
    const detected = await conflictingRows(pool, table, changed);
    if (detected.rows.length) {
      conflictCount = detected.rows.length;
      conflictDetails = detected.details;
      const conflictKeys = new Set(detected.rows.map((item) => item.k.toLowerCase()));
      applicable = changed.filter((row) => !conflictKeys.has(row.k.toLowerCase()));
    }
  }

  if (!applicable.length) {
    return {
      changed: changedCount,
      inserted: 0,
      updated: 0,
      conflicts: conflictCount,
      conflictDetails,
      ...(seeded ? {} : { seeded: changedCount, notInApp: Number((await pool.request().query(st.missingInApp)).recordset[0]?.n || 0) }),
    };
  }

  const withCounts = (inserted: number, updated: number): Result => ({
    changed: changedCount,
    inserted,
    updated,
    conflicts: conflictCount,
    conflictDetails,
    ...(seeded ? {} : { seeded: changedCount, notInApp: Number(0) }),
  });

  try {
    const result = await applyBatch(pool, table, st.apply, applicable);
    const summary = withCounts(result.inserted, result.updated);
    if (!seeded) summary.notInApp = Number((await pool.request().query(st.missingInApp)).recordset[0]?.n || 0);
    return summary;
  } catch (err: any) {
    console.warn(`  ${table.name.padEnd(26)} batch rejected (${err?.message || err}); applying row by row`);
  }
  const res = withCounts(0, 0);
  for (const row of applicable) {
    try {
      const r = await applyBatch(pool, table, st.apply, [row]);
      res.inserted += r.inserted;
      res.updated += r.updated;
    } catch (err: any) {
      res.conflicts++;
      console.warn(`  ${table.name.padEnd(26)} deferred Live row ${row.k} until the next run: ${err?.message || err}`);
    }
  }
  if (!seeded) res.notInApp = Number((await pool.request().query(st.missingInApp)).recordset[0]?.n || 0);
  return res;
}

async function syncProductBankSetting(pool: Pool) {
  const key = PRODUCT_BANK_SETTING;
  const sourceRows = await pool.request().input("key", sql.NVarChar(200), key).query(`
    SELECT [value] FROM [${SOURCE}].dbo.portal_settings WHERE [key] = @key`);
  if (!sourceRows.recordset[0]?.value) {
    throw new Error(`Live portal setting ${key} is missing.`);
  }
  const targetRows = await pool.request().input("key", sql.NVarChar(200), key).query(`
    SELECT [value] FROM dbo.portal_settings WHERE [key] = @key`);
  const parseConfig = (raw: unknown, database: string): Record<string, unknown> => {
    if (raw == null || raw === "") return {};
    let value: unknown;
    try {
      value = JSON.parse(String(raw));
    } catch (err) {
      throw new Error(`${database} portal setting ${key} is not valid JSON.`);
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`${database} portal setting ${key} must be a JSON object.`);
    }
    return value as Record<string, unknown>;
  };
  const sourceConfig = parseConfig(sourceRows.recordset[0].value, SOURCE);
  const targetConfig = parseConfig(targetRows.recordset[0]?.value, "target");
  const merged = { ...targetConfig, ...sourceConfig };
  const value = JSON.stringify(merged);
  if (targetRows.recordset[0]?.value === value) {
    return { added: 0, updated: 0, preserved: Object.keys(targetConfig).filter((product) => !(product in sourceConfig)).length };
  }

  const tx = new sql.Transaction(pool);
  await tx.begin();
  try {
    const request = new sql.Request(tx).input("key", sql.NVarChar(200), key).input("value", sql.NVarChar(sql.MAX), value);
    await request.query(`
      UPDATE dbo.portal_settings SET [value] = @value, updated_at = SYSUTCDATETIME() WHERE [key] = @key;
      IF @@ROWCOUNT = 0
        INSERT INTO dbo.portal_settings ([key], [value], updated_at) VALUES (@key, @value, SYSUTCDATETIME());`);
    if (DRY_RUN) await tx.rollback();
    else await tx.commit();
  } catch (err) {
    await tx.rollback().catch(() => {});
    throw err;
  }

  const added = Object.keys(sourceConfig).filter((product) => !(product in targetConfig)).length;
  const updated = Object.keys(sourceConfig).filter(
    (product) => product in targetConfig && JSON.stringify(targetConfig[product]) !== JSON.stringify(sourceConfig[product])
  ).length;
  return { added, updated, preserved: Object.keys(targetConfig).filter((product) => !(product in sourceConfig)).length };
}

async function syncOnce(): Promise<number> {
  if (!/^[A-Za-z0-9_]+$/.test(SOURCE)) throw new Error(`Invalid SYNC_SOURCE_DATABASE "${SOURCE}"`);
  const started = Date.now();
  const pool = await connect(resolveMssqlTarget());
  try {
    const target = String((await pool.request().query("SELECT DB_NAME() AS db")).recordset[0].db);
    if (target.toLowerCase() === SOURCE.toLowerCase()) {
      throw new Error(`Target ${target} is the source database; this job only writes the app database.`);
    }
    const state = await pool.request().query("SELECT OBJECT_ID(N'dbo.__live_sync_rows', N'U') AS id");
    if (!state.recordset[0]?.id) throw new Error("dbo.__live_sync_rows is missing; apply migration 007 first.");
    console.log(`[live-to-dev] ${new Date(started).toISOString()} ${SOURCE} -> ${target}${DRY_RUN ? " (dry run)" : ""}`);

    const adopted = await adoptLiveIdentities(pool);
    if (adopted.employees || adopted.tests || adopted.questions || adopted.sessions) {
      console.log(
        `  identity remap               employees ${adopted.employees}, tests ${adopted.tests}, questions ${adopted.questions}, sessions ${adopted.sessions}${DRY_RUN ? " (dry run, rolled back)" : ""}`
      );
    }

    const totals = { changed: 0, inserted: 0, updated: 0, conflicts: 0 };
    let failed = 0;
    const selected = selectTables(argValue(argv, "tables"));
    for (const table of selected.filter((t) => !EXCLUDED.has(t.name))) {
      try {
        const r = await syncTable(pool, table);
        if (typeof r === "string") {
          console.log(`  ${table.name.padEnd(26)} ${r}`);
          continue;
        }
        if (r.seeded !== undefined) {
          totals.changed += r.changed;
          totals.inserted += r.inserted;
          totals.updated += r.updated;
          totals.conflicts += r.conflicts;
          console.log(
            `  ${table.name.padEnd(26)} bootstrap: recorded ${r.seeded} Live row(s), +${r.inserted} inserted, ~${r.updated} updated${r.conflicts ? `, ${r.conflicts} waiting on a parent row` : ""}${r.notInApp ? `; ${r.notInApp} Live row(s) remain missing` : ""}`
          );
          continue;
        }
        for (const k of Object.keys(totals) as Array<keyof typeof totals>) totals[k] += r[k];
        if (r.changed || r.conflicts) {
          console.log(
            `  ${table.name.padEnd(26)} ${String(r.changed).padStart(5)} changed in Live | +${r.inserted} inserted, ~${r.updated} updated${r.conflicts ? `, ${r.conflicts} waiting on a parent row` : ""}`
          );
        }
        if (r.conflictDetails?.length) {
          console.warn(`  ${table.name.padEnd(26)} notes: ${[...new Set(r.conflictDetails)].join(", ")}`);
        }
      } catch (err: any) {
        failed++;
        console.error(`  ${table.name.padEnd(26)} FAILED: ${err?.message || err}`);
      }
    }
    if (selected.some((table) => table.name === "employee_portal_question_bank")) {
      try {
        const setting = await syncProductBankSetting(pool);
        console.log(
          `  ${PRODUCT_BANK_SETTING.padEnd(26)} merged from Live: +${setting.added} product(s), ~${setting.updated} updated, ${setting.preserved} Dev-only preserved${DRY_RUN ? " (dry run)" : ""}`
        );
      } catch (err: any) {
        failed++;
        console.error(`  ${PRODUCT_BANK_SETTING.padEnd(26)} FAILED: ${err?.message || err}`);
      }
    }
    const secs = ((Date.now() - started) / 1000).toFixed(1);
    console.log(
      `[live-to-dev] ${DRY_RUN ? "dry run, rolled back" : "done"}: ${totals.changed} Live row(s) new/changed, +${totals.inserted} inserted, ~${totals.updated} updated, ${totals.conflicts} waiting on a parent row${failed ? `, ${failed} table(s) failed` : ""}, ${secs}s`
    );
    return failed;
  } finally {
    await pool.close();
  }
}

async function main() {
  const minutes = intervalMinutes();
  let stop = false;
  const halt = () => {
    stop = true;
  };
  process.on("SIGINT", halt);
  process.on("SIGTERM", halt);
  do {
    const failed = await syncOnce();
    if (failed) process.exitCode = 1;
    if (!minutes || stop) break;
    console.log(`[live-to-dev] next run in ${minutes} minute(s)`);
    const deadline = Date.now() + minutes * 60_000;
    while (!stop && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(1_000, deadline - Date.now())));
    }
  } while (!stop);
}

main().catch((err) => {
  console.error(`[live-to-dev] FAILED: ${err?.message || err}`);
  process.exit(1);
});
