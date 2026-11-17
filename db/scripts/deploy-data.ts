/**
 * Make MSSQL table data match mssql/data/<table>.json.
 *
 * All tables are applied in ONE transaction: if any row is invalid, nothing changes.
 * Rows are matched on primary key; missing rows are inserted, changed rows updated, identical rows left alone.
 * Existing rows are never deleted.
 *
 * Usage:
 *   npm run db:deploy-data -- --target=local
 *   npm run db:deploy-data -- --target=remote --dry-run        (apply, report, roll back)
 *   npm run db:deploy-data -- --tables=employees,portal_settings
 */
import { existsSync, readFileSync } from "fs";
import { applyTargetArg, argValue, loadProjectEnv } from "./lib/env";
import { connect, resolveMssqlTarget, sql } from "./lib/mssql";
import { dataFile, quoteIdent, selectTables, type TableDef } from "./lib/tables";

loadProjectEnv();
const argv = process.argv.slice(2);
applyTargetArg(argv);

const DRY_RUN = argv.includes("--dry-run");

type ColumnInfo = {
  name: string;
  dataType: string;
  maxLength: number | null;
  precision: number | null;
  scale: number | null;
  dtPrecision: number | null;
};

type Row = Record<string, unknown>;
type Plan = { table: TableDef; rows: Row[]; columns: ColumnInfo[]; stage: string };

async function loadSchema(pool: sql.ConnectionPool): Promise<Map<string, Map<string, ColumnInfo>>> {
  const result = await pool.request().query(`
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, DATETIME_PRECISION
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo'
ORDER BY TABLE_NAME, ORDINAL_POSITION`);
  const schema = new Map<string, Map<string, ColumnInfo>>();
  for (const r of result.recordset) {
    const table = String(r.TABLE_NAME);
    if (!schema.has(table)) schema.set(table, new Map());
    schema.get(table)!.set(String(r.COLUMN_NAME), {
      name: String(r.COLUMN_NAME),
      dataType: String(r.DATA_TYPE).toLowerCase(),
      maxLength: r.CHARACTER_MAXIMUM_LENGTH,
      precision: r.NUMERIC_PRECISION,
      scale: r.NUMERIC_SCALE,
      dtPrecision: r.DATETIME_PRECISION,
    });
  }
  return schema;
}

function sqlTypeDdl(c: ColumnInfo): string {
  const len = (n: number | null) => (n == null || n < 0 ? "MAX" : String(n));
  switch (c.dataType) {
    case "nvarchar":
    case "varchar":
    case "nchar":
    case "char":
    case "varbinary":
    case "binary":
      return `${c.dataType.toUpperCase()}(${len(c.maxLength)})`;
    case "decimal":
    case "numeric":
      return `${c.dataType.toUpperCase()}(${c.precision},${c.scale})`;
    case "datetimeoffset":
    case "datetime2":
    case "time":
      return `${c.dataType.toUpperCase()}(${c.dtPrecision ?? 7})`;
    default:
      return c.dataType.toUpperCase();
  }
}

function driverType(c: ColumnInfo): sql.ISqlType | (() => sql.ISqlType) {
  const len = c.maxLength == null || c.maxLength < 0 ? sql.MAX : c.maxLength;
  switch (c.dataType) {
    case "uniqueidentifier": return sql.UniqueIdentifier;
    case "nvarchar": return sql.NVarChar(len);
    case "varchar": return sql.VarChar(len);
    case "nchar": return sql.NChar(len);
    case "char": return sql.Char(len);
    case "int": return sql.Int;
    case "bigint": return sql.BigInt;
    case "smallint": return sql.SmallInt;
    case "tinyint": return sql.TinyInt;
    case "bit": return sql.Bit;
    case "decimal":
    case "numeric": return sql.Decimal(c.precision ?? 18, c.scale ?? 0);
    case "float": return sql.Float;
    case "real": return sql.Real;
    case "datetimeoffset": return sql.DateTimeOffset(c.dtPrecision ?? 7);
    case "datetime2": return sql.DateTime2(c.dtPrecision ?? 7);
    case "datetime": return sql.DateTime;
    case "date": return sql.Date;
    default: throw new Error(`Unsupported column type ${c.dataType} (${c.name})`);
  }
}

function toDbValue(c: ColumnInfo, value: unknown): unknown {
  if (value === null || value === undefined) return null;
  switch (c.dataType) {
    case "nvarchar":
    case "varchar":
    case "nchar":
    case "char": {
      const s = typeof value === "object" ? JSON.stringify(value) : String(value);
      if (c.maxLength != null && c.maxLength > 0 && s.length > c.maxLength) {
        throw new Error(`value is ${s.length} characters, column allows ${c.maxLength}`);
      }
      return s;
    }
    case "uniqueidentifier":
      return String(value);
    case "bit":
      return value === true || value === 1 || value === "1" || String(value).toLowerCase() === "true";
    case "int":
    case "bigint":
    case "smallint":
    case "tinyint":
    case "decimal":
    case "numeric":
    case "float":
    case "real": {
      const n = Number(value);
      if (!Number.isFinite(n)) throw new Error(`"${value}" is not a number`);
      return n;
    }
    case "datetimeoffset":
    case "datetime2":
    case "datetime":
    case "date": {
      const d = new Date(String(value));
      if (Number.isNaN(d.getTime())) throw new Error(`"${value}" is not a valid date`);
      return d;
    }
    default:
      return value;
  }
}

function readRows(table: TableDef): Row[] {
  const raw = readFileSync(dataFile(table.name), "utf8").replace(/^\uFEFF/, "");
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err: any) {
    throw new Error(`${dataFile(table.name)} is not valid JSON: ${err?.message || err}`);
  }
  if (!Array.isArray(parsed)) throw new Error(`${dataFile(table.name)} must be a JSON array of row objects.`);
  return parsed as Row[];
}

/** Catch bad edits with a readable message before touching the database. */
function buildPlan(table: TableDef, schema: Map<string, Map<string, ColumnInfo>>): Plan {
  const rows = readRows(table);
  const tableCols = schema.get(table.name);
  if (!tableCols) {
    throw new Error(`Table dbo.${table.name} does not exist on the target. Add a migration in mssql/migrations first.`);
  }

  const used = new Set<string>(table.pk);
  const seen = new Set<string>();
  rows.forEach((row, i) => {
    if (row == null || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(`${table.name}.json row #${i + 1} is not an object.`);
    }
    for (const k of Object.keys(row)) used.add(k);
    const key = table.pk.map((c) => row[c]);
    if (key.some((v) => v === null || v === undefined || v === "")) {
      throw new Error(`${table.name}.json row #${i + 1} is missing primary key ${table.pk.join(", ")}.`);
    }
    const keyText = key.map((v) => String(v).toLowerCase()).join("|");
    if (seen.has(keyText)) throw new Error(`${table.name}.json has duplicate primary key ${keyText}.`);
    seen.add(keyText);
  });

  const unknown = [...used].filter((c) => !tableCols.has(c));
  if (unknown.length) {
    throw new Error(
      `${table.name}.json has column(s) not in dbo.${table.name}: ${unknown.join(", ")}. ` +
        `Add them with a new migration (ALTER TABLE) or remove them from the data file.`
    );
  }

  const columns = [...tableCols.values()].filter((c) => used.has(c.name));
  return { table, rows, columns, stage: `#stage_${table.name}` };
}

async function stage(tx: sql.Transaction, plan: Plan): Promise<void> {
  const ddl = plan.columns.map((c) => `${quoteIdent(c.name)} ${sqlTypeDdl(c)} NULL`).join(", ");
  await new sql.Request(tx).batch(`CREATE TABLE ${plan.stage} (${ddl});`);
  if (!plan.rows.length) return;

  const t = new sql.Table(plan.stage);
  t.create = false;
  for (const c of plan.columns) t.columns.add(c.name, driverType(c) as any, { nullable: true });
  plan.rows.forEach((row, i) => {
    const values = plan.columns.map((c) => {
      try {
        return toDbValue(c, row[c.name]);
      } catch (err: any) {
        throw new Error(`${plan.table.name}.json row #${i + 1}, column ${c.name}: ${err?.message || err}`);
      }
    });
    t.rows.add(...(values as any[]));
  });
  await new sql.Request(tx).bulk(t);
}

function keyMatch(pk: string[], a: string, b: string): string {
  return pk.map((c) => `${a}.${quoteIdent(c)} = ${b}.${quoteIdent(c)}`).join(" AND ");
}

async function merge(tx: sql.Transaction, plan: Plan): Promise<{ inserted: number; updated: number }> {
  if (!plan.rows.length) return { inserted: 0, updated: 0 };
  const target = `dbo.${quoteIdent(plan.table.name)}`;
  const cols = plan.columns.map((c) => quoteIdent(c.name));
  const nonKey = plan.columns.filter((c) => !plan.table.pk.includes(c.name)).map((c) => quoteIdent(c.name));

  const updateClause = nonKey.length
    ? `WHEN MATCHED AND EXISTS (SELECT ${nonKey.map((c) => `s.${c}`).join(", ")} EXCEPT SELECT ${nonKey
        .map((c) => `t.${c}`)
        .join(", ")})
  THEN UPDATE SET ${nonKey.map((c) => `t.${c} = s.${c}`).join(", ")}`
    : "";

  const result = await new sql.Request(tx).query(`
DECLARE @actions TABLE (act NVARCHAR(10));
MERGE ${target} AS t
USING ${plan.stage} AS s ON ${keyMatch(plan.table.pk, "t", "s")}
${updateClause}
WHEN NOT MATCHED BY TARGET THEN
  INSERT (${cols.join(", ")}) VALUES (${cols.map((c) => `s.${c}`).join(", ")})
OUTPUT $action INTO @actions;
SELECT
  SUM(CASE WHEN act = 'INSERT' THEN 1 ELSE 0 END) AS inserted,
  SUM(CASE WHEN act = 'UPDATE' THEN 1 ELSE 0 END) AS updated
FROM @actions;`);
  const r = result.recordset[0] || {};
  return { inserted: Number(r.inserted || 0), updated: Number(r.updated || 0) };
}

async function main() {
  if (argv.includes("--prune")) {
    throw new Error("--prune is not supported; data deployment never deletes existing rows.");
  }
  const target = resolveMssqlTarget();
  const tableFilter = argValue(argv, "tables");
  const requestedTables = selectTables(tableFilter);
  const missing = requestedTables.filter((table) => !existsSync(dataFile(table.name)));
  if (tableFilter && missing.length) {
    throw new Error(
      `Missing seed dataset(s): ${missing.map((table) => dataFile(table.name)).join(", ")}. ` +
        "Seed JSON files are excluded from Git and Docker; supply approved data files before deploying seeds."
    );
  }
  const tables = requestedTables.filter((table) => existsSync(dataFile(table.name)));
  if (!tables.length) {
    throw new Error(
      "No seed datasets found in mssql/data. Seed JSON files are excluded from Git and Docker; " +
        "supply approved data files before deploying seeds."
    );
  }
  const pool = await connect(target);

  try {
    const schema = await loadSchema(pool);
    const plans = tables.map((t) => buildPlan(t, schema));
    console.log(
      `Deploying data to ${target}${DRY_RUN ? " (dry run, will roll back)" : ""}: ` +
        `${plans.length} table(s), ${plans.reduce((n, p) => n + p.rows.length, 0)} row(s)`
    );

    const tx = new sql.Transaction(pool);
    await tx.begin();
    try {
      for (const plan of plans) await stage(tx, plan);

      let changes = 0;
      for (const plan of plans) {
        const { inserted, updated } = await merge(tx, plan);
        changes += inserted + updated;
        const parts = [`+${inserted} inserted`, `~${updated} updated`];
        console.log(`  ${plan.table.name.padEnd(26)} ${String(plan.rows.length).padStart(6)} rows | ${parts.join(", ")}`);
      }

      if (DRY_RUN) {
        await tx.rollback();
        console.log(`Dry run: ${changes} change(s) would be applied. Rolled back.`);
      } else {
        await tx.commit();
        console.log(`Data deploy complete on ${target}: ${changes} change(s).`);
      }
    } catch (err) {
      await tx.rollback().catch(() => {});
      throw err;
    }
  } finally {
    await pool.close();
  }
}

main().catch((err) => {
  console.error(err?.message || err);
  process.exit(1);
});
